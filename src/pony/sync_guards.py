"""When a pass that uploads is a lost index rather than a busy user.

Both sync engines ask this, and they ask it about the same signal.  A
row that should exist on the server but carries no handle for it --
``uid IS NULL`` in the mail index, ``href IS NULL`` in the calendar's --
cannot say whether it was composed here or whether it is on the server
and the handle was lost.  An index rebuilt without its handles looks
exactly like a user who wrote a great deal offline, and uploading it
would put a duplicate of every one of those rows on the server.

So a pass whose uploads would rewrite a large share of a collection is
held.  The rule and its three numbers live here rather than once per
engine: they were written out twice, identically, under two sets of
names, while `ai/calendar/SYNCHRONIZATION.md` said the numbers did not
line up across the two halves.  One of those had to be wrong.

The ratio is **half** the collection and not a fifth, because a row
without a handle is also what every locally composed message and every
locally created event looks like until it is pushed -- a fifth of a
small folder is a morning's work.  The share worth catching is near all
of it.
"""

from __future__ import annotations

#: Below this many uploads, nothing is held: a handful is ordinary use.
MASS_UPLOAD_MIN = 5
#: Held once the uploads are more than this share of what the
#: collection is known to hold.  Catches a small collection, where an
#: absolute ceiling never fires.
MASS_UPLOAD_RATIO = 0.50
#: Held at this many uploads whatever the share, which is what catches a
#: large collection.  A collection the server holds nothing in falls to
#: this alone, so writing a new one offline still works.
MASS_UPLOAD_ABSOLUTE = 50


def holds_mass_upload(*, push_count: int, known: int) -> bool:
    """Whether *push_count* uploads against *known* on the server is held.

    *known* is what the collection is known to hold: for the mail half
    the rows already carrying a UID plus what this pass will fetch or
    re-key, and for the calendar what one ``calendar-query`` reports.
    Zero means nothing is known, and then only the absolute ceiling can
    fire -- which is the point, since the case worth catching is the one
    where no local row remembers its handle and a ratio cannot be
    computed at all.
    """
    if push_count < MASS_UPLOAD_MIN:
        return False
    over_ratio = bool(known) and push_count > known * MASS_UPLOAD_RATIO
    return push_count >= MASS_UPLOAD_ABSOLUTE or over_ratio


__all__ = [
    "MASS_UPLOAD_ABSOLUTE",
    "MASS_UPLOAD_MIN",
    "MASS_UPLOAD_RATIO",
    "holds_mass_upload",
]
