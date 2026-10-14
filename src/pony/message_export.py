"""Saving a message and its parts, numbered the same way everywhere.

A message is a numbered list of things a reader can put on disk: part
``0`` is the message itself, as it arrived, and parts ``1..N`` are its
attachments in the order the message view lists them.  One numbering,
so ``s`` (save the message) is the same operation as choosing ``0``
under ``S``, and so the terminal and the browser offer the same
vocabulary instead of each inventing one.

The operations live here rather than in a screen for the reason the rest
of the action modules do: they are I/O over a message, they report
through :class:`pony.calendar.outcome.Outcome`, and both front ends need them.
What they report is deliberately specific -- the names actually written
and where -- because "Saved 3 file(s)" leaves a user to go and look.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pony.calendar.outcome import Notice, Outcome, failure, info, warn

from .domain import ViewerRule
from .message_renderer import (
    AttachmentPayload,
    extract_attachment,
    fmt_size,
    proposed_filename,
    unique_destination,
)
from .paths import contain

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .message_renderer import RenderedMessage

__all__ = [
    "TEXT_BODY",
    "WHOLE_MESSAGE",
    "MessagePart",
    "PartKey",
    "OpenablePart",
    "downloads_dir",
    "listed_parts",
    "message_parts",
    "parse_part_selection",
    "part_key",
    "part_payload",
    "parts_to_open",
    "save_parts",
    "viewer_command",
]

#: The message itself, as it arrived.  Attachments are numbered from 1.
WHOLE_MESSAGE: Final = 0

#: The body as text, with no HTML in it -- whatever the sender sent.
TEXT_BODY: Final = "T"

#: What names one part: a number, or ``T`` for the text body.
type PartKey = int | str


def part_key(text: str) -> PartKey | None:
    """Read one part name, or ``None`` when it names nothing.

    The one parser, so the prompt, the digit accelerators and a URL all
    agree about what ``0`` and ``T`` mean.  Case is not significant:
    ``t`` is the key a user without a shift held is pressing.
    """
    token = text.strip()
    if token.upper() == TEXT_BODY:
        return TEXT_BODY
    try:
        return int(token)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class MessagePart:
    """One named thing a message can be saved or opened as."""

    key: PartKey
    filename: str
    content_type: str
    size_bytes: int

    @property
    def is_whole_message(self) -> bool:
        return self.key == WHOLE_MESSAGE

    @property
    def is_text_body(self) -> bool:
        return self.key == TEXT_BODY

    @property
    def label(self) -> str:
        """How a picker names this part: key, name and size."""
        return f"{self.key}: {self.filename} ({fmt_size(self.size_bytes)})"


def message_parts(rendered: RenderedMessage) -> tuple[MessagePart, ...]:
    """Every part of *rendered*: the message, its text, its attachments."""
    text = _text_payload(rendered)
    return (
        MessagePart(
            key=WHOLE_MESSAGE,
            filename=proposed_filename(rendered, suffix=".eml"),
            content_type="message/rfc822",
            size_bytes=len(rendered.raw_bytes),
        ),
        MessagePart(
            key=TEXT_BODY,
            filename=text.filename,
            content_type=text.content_type,
            size_bytes=text.size_bytes,
        ),
        *(
            MessagePart(
                key=attachment.index,
                filename=attachment.filename,
                content_type=attachment.content_type,
                size_bytes=attachment.size_bytes,
            )
            for attachment in rendered.attachments
        ),
    )


def listed_parts(rendered: RenderedMessage) -> tuple[MessagePart, ...]:
    """The parts a reader sees listed: the attachments, ``1..N``.

    Parts ``0`` and ``T`` are reachable by name everywhere -- ``s``,
    the ``S`` prompt, a digit accelerator, ``n=0`` in a URL -- so
    listing them put two rows naming the open message on every message
    that had an attachment.  Both front ends ask here rather than each
    deciding, so the lists stay the same list.
    """
    return tuple(
        part
        for part in message_parts(rendered)
        if not part.is_whole_message and not part.is_text_body
    )


def parse_part_selection(text: str, *, total: int) -> list[PartKey] | None:
    """Read a part selection, or ``None`` when it names nothing.

    Part ``0`` is the message itself, ``T`` is its body as text with no
    HTML in it, and ``1..total`` are its attachments.

    Accepted forms:
        "*"         -> every part (``[0, "T", 1..total]``)
        "0"         -> ``[0]``, the message as it arrived
        "T"         -> ``["T"]``, the body as text (``t`` too)
        "1,3, 5"    -> ``[1, 3, 5]`` (whitespace tolerated)
        "1-3"       -> ``[1, 2, 3]`` (inclusive range)
        "0,T,1-3"   -> the lot, named one way or another

    Returns ``None`` on empty input, unknown tokens, malformed or
    reversed ranges, out-of-range indices, or duplicates -- the caller
    surfaces the error.

    This lived in the terminal's picker screen, where the browser could
    not reach it: ``S`` there took a single part and answered anything
    else with a bare "Which part?" page, so the grammar the prompt
    advertised worked in one front end only.
    """
    text = text.strip()
    if not text:
        return None
    if text == "*":
        return [WHOLE_MESSAGE, TEXT_BODY, *range(1, total + 1)]
    parts: list[PartKey] = []
    for token in (t.strip() for t in text.split(",")):
        if not token:
            continue
        if "-" in token:
            lo_text, _, hi_text = token.partition("-")
            try:
                lo, hi = int(lo_text.strip()), int(hi_text.strip())
            except ValueError:
                return None
            if lo > hi:
                return None
            parts.extend(range(lo, hi + 1))
            continue
        key = part_key(token)
        if key is None:
            return None
        parts.append(key)
    if not parts:
        return None
    if any(isinstance(key, int) and not 0 <= key <= total for key in parts):
        return None
    if len(set(parts)) != len(parts):
        return None
    return parts


def _text_payload(rendered: RenderedMessage) -> AttachmentPayload:
    """The body as text, which is what the reader itself shows.

    ``RenderedMessage.body`` is already the HTML-free reading of the
    message -- an HTML-only message is stripped to text on the way in,
    links carried as text beside it -- so this is the one part whose
    bytes Pony produced rather than received.
    """
    data = rendered.body.encode("utf-8")
    return AttachmentPayload(
        filename=proposed_filename(rendered, suffix=".txt"),
        content_type="text/plain",
        size_bytes=len(data),
        data=data,
    )


def part_payload(
    rendered: RenderedMessage,
    key: PartKey,
) -> AttachmentPayload | None:
    """The bytes of part *key*, or ``None`` when there is no such part.

    Part ``0`` is the message as it arrived -- the same bytes the
    browser serves for a raw download, under the same proposed name --
    ``T`` is its body as text, and the rest come from the attachment
    walk, which is the one place that decides what counts as an
    attachment.
    """
    if key == TEXT_BODY:
        return _text_payload(rendered)
    if key == WHOLE_MESSAGE:
        return AttachmentPayload(
            filename=proposed_filename(rendered, suffix=".eml"),
            content_type="message/rfc822",
            size_bytes=len(rendered.raw_bytes),
            data=rendered.raw_bytes,
        )
    if not isinstance(key, int):
        return None
    return extract_attachment(rendered.raw_bytes, key)


def downloads_dir(configured: Path | None) -> Path:
    """Where a saved part goes when the configuration does not say.

    ``~/Downloads``, which is what the platform's own file managers
    use.  This was the reader screen's answer, so every key that writes
    a part asked the screen for it and a second front end would have
    had to decide again.

    The fallback is contained: under ``PONY_ROOT`` it is that
    directory's image inside the sandbox, because what ``PONY_ROOT``
    promises is that every directory a run resolves moves underneath it
    (see :mod:`pony.paths`).  Reaching for the home directory here went
    around that promise, so the suite wrote into the real
    ``~/Downloads`` -- and read it, since the save-folder picker opens
    there.  A path a caller or a configuration names is its own answer
    and is left alone.
    """
    return configured or contain(Path.home() / "Downloads")


@dataclass(frozen=True, slots=True)
class OpenablePart:
    """One part written out, and what should show it.

    ``path`` is where the bytes were written, or ``None`` for a message
    -- part 0, or an attached one -- which a reader shows itself rather
    than handing to whatever the desktop opens an ``.eml`` with.
    ``command`` is the argv the ``[viewers]`` table names for this
    content type, or ``None`` for the platform default.
    """

    key: PartKey
    content_type: str
    data: bytes
    path: Path | None
    command: tuple[str, ...] | None


def parts_to_open(
    rendered: RenderedMessage,
    keys: Sequence[PartKey],
    dest_dir: Path,
    *,
    viewers: Sequence[ViewerRule] = (),
) -> tuple[tuple[OpenablePart, ...], Outcome]:
    """Write the chosen parts out and say what should show each one.

    Every decision here is the model's: that a message goes to the
    reader's own viewer rather than to the desktop's, that everything
    else is written beside the attachments under a name not already
    taken, and which viewer command a content type gets.  Which parts
    were not there and which could not be written come back as notices,
    as they do from :func:`save_parts`.

    Launching is the caller's, and only the caller's: the terminal has
    to suspend itself around a viewer that writes to the same terminal,
    and a browser would serve the bytes instead of running anything.
    """
    if not keys:
        return (), Outcome.refused(warn("Nothing chosen to open."))
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return (), Outcome.refused(failure(f"Could not use {dest_dir}: {exc}"))

    openable: list[OpenablePart] = []
    notices: list[Notice] = []
    missing: list[PartKey] = []
    for key in keys:
        payload = part_payload(rendered, key)
        if payload is None:
            missing.append(key)
            continue
        if payload.content_type == "message/rfc822":
            openable.append(
                OpenablePart(
                    key=key,
                    content_type=payload.content_type,
                    data=payload.data,
                    path=None,
                    command=None,
                )
            )
            continue
        try:
            out = unique_destination(dest_dir, payload.filename)
            out.write_bytes(payload.data)
        except OSError as exc:
            notices.append(failure(f"Could not save part {key}: {exc}"))
            continue
        openable.append(
            OpenablePart(
                key=key,
                content_type=payload.content_type,
                data=payload.data,
                path=out,
                command=viewer_command(viewers, payload.content_type),
            )
        )
    if missing:
        absent = ", ".join(str(key) for key in missing)
        notices.append(warn(f"No part {absent} on this message."))
    return tuple(openable), Outcome(changed=len(openable), notices=tuple(notices))


def viewer_command(
    viewers: Sequence[ViewerRule], content_type: str | None
) -> tuple[str, ...] | None:
    """The configured argv for *content_type*, or ``None`` for the default.

    Matching is exact on the lower-cased type, so a ``text/calendar``
    entry does not claim ``text/plain``.
    """
    if not content_type:
        return None
    wanted = content_type.strip().lower()
    for rule in viewers:
        if rule.content_type == wanted:
            return rule.command
    return None


def save_parts(
    rendered: RenderedMessage,
    keys: Sequence[PartKey],
    dest_dir: Path,
) -> Outcome:
    """Write the chosen parts into *dest_dir*, naming each one written.

    Every part is attempted: one unwritable file does not abandon the
    rest, and the notices say which were written, which were not there
    and which failed.  A name already taken gets ``-1`` appended rather
    than overwriting what is there, so the notice names the file that
    now exists and not the one that was asked for.
    """
    if not keys:
        return Outcome.refused(warn("Nothing chosen to save."))
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return Outcome.refused(failure(f"Could not use {dest_dir}: {exc}"))

    written: list[str] = []
    notices: list[Notice] = []
    missing: list[PartKey] = []
    for key in keys:
        payload = part_payload(rendered, key)
        if payload is None:
            missing.append(key)
            continue
        try:
            out = unique_destination(dest_dir, payload.filename)
            out.write_bytes(payload.data)
        except OSError as exc:
            notices.append(failure(f"Could not save part {key}: {exc}"))
            continue
        written.append(out.name)
    if written:
        notices.insert(0, info(f"Saved {', '.join(written)} to {dest_dir}."))
    if missing:
        absent = ", ".join(str(key) for key in missing)
        notices.append(warn(f"No part {absent} on this message."))
    return Outcome(changed=len(written), notices=tuple(notices))
