"""Saving a message and its parts, numbered the same way everywhere.

A message is a numbered list of things a reader can put on disk: part
``0`` is the message itself, as it arrived, and parts ``1..N`` are its
attachments in the order the message view lists them.  One numbering,
so ``s`` (save the message) is the same operation as choosing ``0``
under ``S``, and so the terminal and the browser offer the same
vocabulary instead of each inventing one.

The operations live here rather than in a screen for the reason the rest
of the action modules do: they are I/O over a message, they report
through :class:`chronos.outcome.Outcome`, and both front ends need them.
What they report is deliberately specific -- the names actually written
and where -- because "Saved 3 file(s)" leaves a user to go and look.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from chronos.outcome import Notice, Outcome, failure, info, warn

from .message_renderer import (
    AttachmentPayload,
    extract_attachment,
    fmt_size,
    proposed_filename,
    unique_destination,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from .message_renderer import RenderedMessage

__all__ = [
    "TEXT_BODY",
    "WHOLE_MESSAGE",
    "MessagePart",
    "PartKey",
    "message_parts",
    "part_key",
    "part_payload",
    "save_parts",
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
    def note(self) -> str:
        """What this part is, for a reader who has not met the numbering."""
        if self.is_whole_message:
            return "the message itself"
        if self.is_text_body:
            return "the body as text"
        return ""

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
