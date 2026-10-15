"""Which names a sync covers, and which of them it may write back to.

The mail half matches folder names and the calendar half matches
calendar names, but both ask the same three questions of the same three
lists, so the answers live here rather than once per half -- where they
had drifted into opposite meanings. ``include`` was an exception to
``exclude`` on one side and a mandatory whitelist on the other, so
``exclude = [".*"]`` with ``include = ["INBOX"]`` synced one folder for
mail and nothing at all for the calendar, and ``include = []`` meant
"no exceptions" for one and "no calendars" for the other.

One rule, in this order:

1. a name ``exclude`` matches is out of scope,
2. unless ``include`` matches it as well: ``include`` is an *exception*
   to ``exclude``, never a whitelist, so an empty ``include`` means "no
   exceptions" and not "nothing",
3. ``read_only`` only marks. It decides whether what is synced may be
   written back to the server, and never rescues a name from
   ``exclude``,
4. a name none of the three lists mentions is synced, and writable.

Patterns are Python regexes matched against the whole name with
``re.fullmatch``, with ``*`` accepted as a shorthand for ``.*`` because
someone writing a folder pattern reaches for a glob first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Patterns that match every name there is. ``*`` is the glob
#: shorthand; the others are what gets written when the intent is
#: "everything". Recognising them is what lets `unrescued_read_only`
#: tell a configuration that excludes wholesale from one that names a
#: few folders.
CATCH_ALL = frozenset({"*", ".*", ".+"})


def matches(patterns: tuple[str, ...], name: str) -> bool:
    """Does any of `patterns` match the whole of `name`?"""
    return any(
        True if pattern == "*" else re.fullmatch(pattern, name) is not None
        for pattern in patterns
    )


def pattern_problem(pattern: str) -> str | None:
    """Why `pattern` cannot be used as a scope pattern, or None.

    Both halves validate at load time and name the offending entry, so
    a typo is a configuration error rather than a crash part-way
    through a sync. That means they have to agree on what counts as a
    pattern -- ``*`` is accepted and is not a valid regex -- so the
    test is written once and both callers wrap the answer in their own
    error type.
    """
    if not pattern:
        return "must be a non-empty string"
    if pattern == "*":
        return None
    try:
        re.compile(pattern)
    except re.error as exc:
        return f"invalid regex '{pattern}': {exc}"
    return None


@dataclass(frozen=True, slots=True)
class SyncScope:
    """The three lists that decide what a sync covers.

    The rule is in the module docstring. ``include`` and ``exclude``
    answer *whether* a name is synced; ``read_only`` answers what may
    be done with it, which is a separate question and kept separate.
    """

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    read_only: tuple[str, ...] = ()

    def should_sync(self, name: str) -> bool:
        """Is `name` in scope at all?

        Nothing is excluded by default: a name no pattern mentions is
        synced, and ``include`` is consulted only to rescue one that
        ``exclude`` caught.
        """
        if matches(self.exclude, name):
            return matches(self.include, name)
        return True

    def is_read_only(self, name: str) -> bool:
        """Is `name` synced server-to-local only?

        A name ``exclude`` swallows is not synced at all, so this says
        nothing useful about it; ask `should_sync` first.
        """
        return matches(self.read_only, name)

    def unrescued_read_only(self) -> tuple[str, ...]:
        """``read_only`` patterns that a wholesale ``exclude`` swallows.

        ``read_only`` used to rescue a name from ``exclude`` in the mail
        half, so ``exclude = [".*"]`` with ``read_only = ["Sent"]``
        synced Sent read-only. It does not any more, and a
        configuration written that way would quietly stop syncing a
        folder -- so the loader refuses it and says what to add.

        Only the decidable shape is reported: a catch-all ``exclude``,
        and a ``read_only`` pattern that ``include`` does not repeat.
        Whether two narrower regexes overlap is not a question worth
        answering here, so this reads a configuration rather than
        proving anything about it.
        """
        if not any(pattern in CATCH_ALL for pattern in self.exclude):
            return ()
        return tuple(
            pattern for pattern in self.read_only if pattern not in self.include
        )
