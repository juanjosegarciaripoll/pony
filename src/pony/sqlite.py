"""SQLite plumbing both indexes are built on.

Mail and calendar each keep their own index in their own database, with
their own schema and their own repository class, but the way a
connection is held and a timestamp is stored is the same work twice
over: one connection per thread with the transaction state beside it
(:class:`ThreadLocalSqlite`), an ISO 8601 round trip in UTC
(:func:`datetime_to_sql`, :func:`sql_to_datetime`), and chunking a list
of ids so an ``IN`` clause stays under SQLite's parameter limit
(:func:`chunked`).

Nothing here knows what is being stored.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from itertools import batched
from pathlib import Path
from typing import cast

# Host parameters per statement, kept well under SQLite's own limit
# (999 before 3.32, 32,766 after) so an ``IN (…)`` built from a caller's
# list is never the thing that fails.
_MAX_SQL_PARAMETERS = 400

# How many writes an open transaction may accumulate before it commits
# and a new one begins.  Bounded because an unbounded transaction grows
# the WAL until throughput collapses, and loses everything it has done
# if the process is killed; in bounded steps a long rebuild stays both
# fast and resumable.
_COMMIT_EVERY_WRITES = 500

# How long a second writer waits for the lock before giving up.
_BUSY_TIMEOUT_MS = 5000


def sql_to_datetime(value: str | None) -> datetime | None:
    """Parse a stored timestamp back, as UTC.

    A value without an offset is read as UTC, because that is what
    ``datetime_to_sql`` writes; guessing the local zone would move an
    instant by the reader's own offset.
    """
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def datetime_to_sql(value: datetime | None) -> str | None:
    """Render *value* for storage, normalised to UTC.

    One spelling on the way in is what lets the stored text be compared
    as text: ``trashed_at <= cutoff`` and ``received_at DESC`` are both
    lexical, and an offset of ``+02:00`` sorts and compares as a
    different string from the same instant written as ``+00:00``.
    """
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def chunked[T](values: Sequence[T]) -> Iterator[Sequence[T]]:
    """Split *values* into runs that fit in one statement's parameters.

    SQLite caps the number of host parameters per statement, so an
    ``IN (…)`` over an arbitrarily long list has to be issued in pieces.
    The runs stay in the order given, and nothing more: a statement
    with an ``ORDER BY`` sorts each run on its own, so a caller that
    was promised an order has to restore it over the concatenation --
    as ``query_occurrences`` and ``contacts_by_ids`` both do.
    """
    yield from batched(values, _MAX_SQL_PARAMETERS, strict=False)


class ThreadLocalSqlite:
    """One SQLite connection per thread, with per-thread transaction state.

    Whether a transaction is open, and how much work it has accumulated,
    describes *one flow of control*.  Held on the object instead, it is
    shared by every thread that reaches the repository: a write made
    while another thread's batch was open joined that transaction and
    was discarded when the other thread rolled back, while its own
    caller was told it had succeeded.  So the connection and the write
    counter both live in ``threading.local()``, and re-entrancy is read
    off the connection itself (``in_transaction``) rather than from a
    depth counter two threads can decrement out of order.

    Two ways in, because the two have to behave differently:

    ``transaction()`` is a block that happens all at once or not at
    all.  Outside a transaction it opens one and commits on clean exit;
    inside one it is a unit of work, fenced by a ``SAVEPOINT`` so a
    caller that catches the failure of a single operation and carries
    on -- which is what a sync does, per operation -- keeps everything
    the block did before it.  Every ``commit_every`` writes the open
    transaction commits and a new one begins: an unbounded transaction
    grows the WAL until throughput collapses, and loses all of its work
    if the process is killed.

    ``statement()`` is one call that stands alone.  Outside a
    transaction it opens none: in SQLite's autocommit mode the
    statement *is* the transaction, which is both atomic and, unlike a
    deferred ``BEGIN``, retried by the busy handler when another
    connection holds the write lock.  Inside a transaction it is a unit
    of that transaction, exactly as above.  A repository method that
    issues more than one statement therefore asks for a
    ``transaction()``, not a ``statement()``.

    What is left is writer contention: one connection writes at a time,
    so a write that lands while a long batch holds the lock waits for
    ``_BUSY_TIMEOUT_MS`` and then fails with "database is locked".
    Readers are unaffected -- in WAL a reader never waits for a writer
    -- and a batch that commits every ``commit_every`` writes gives the
    waiter its openings.  ``BEGIN IMMEDIATE``, rather than a deferred
    ``BEGIN``, is what puts the wait in the busy handler at all: a
    transaction that reads before it writes (every write here does, via
    the FTS triggers) is refused the upgrade outright, because SQLite
    will not risk the deadlock.

    Connections are opened ``check_same_thread=False`` so that
    ``close()`` can reach the ones other threads opened.
    """

    def __init__(
        self,
        path: Path,
        *,
        commit_every: int = _COMMIT_EVERY_WRITES,
        immediate: bool = True,
    ) -> None:
        self._path = path
        self._commit_every = commit_every
        self._begin = "BEGIN IMMEDIATE" if immediate else "BEGIN"
        self._local = threading.local()
        # Every connection handed out, so close() can walk them, with
        # the generation it was handed out in: a connection a previous
        # close() shut must not be handed back to the thread that
        # cached it, which would see "Cannot operate on a closed
        # database" instead.
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._generation = 0

    def conn(self) -> sqlite3.Connection:
        """This thread's connection, opened on first use."""
        cached = cast(sqlite3.Connection | None, getattr(self._local, "conn", None))
        if cached is not None:
            if self._local.generation == self._generation:
                return cached
            # A close() has been through since.  A thread in the middle
            # of a transaction keeps the connection holding it -- the
            # rest of its block has to land on the same one, and
            # close() left it open for exactly that.  Anything else
            # takes a fresh connection, because its own may be shut.
            with contextlib.suppress(sqlite3.ProgrammingError):
                if cached.in_transaction:
                    return cached
        conn = sqlite3.connect(
            self._path,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        with self._connections_lock:
            self._local.conn = conn
            self._local.generation = self._generation
            self._local.writes = 0
            self._connections.append(conn)
        return conn

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        """Yield this thread's connection inside a transaction.

        Re-entrant: the outermost call owns the transaction, and an
        inner one is a unit of work within it.
        """
        conn = self.conn()
        if conn.in_transaction:
            with self._unit(conn):
                yield conn
            return
        self._local.writes = 0
        conn.execute(self._begin)
        try:
            yield conn
        except BaseException:
            # Suppressed, not reported: SQLite rolls the transaction
            # back itself on a disk-full or I/O error, and then ROLLBACK
            # raises "no transaction is active" — which would be the
            # exception the caller sees instead of the one that
            # actually went wrong.
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    @contextmanager
    def statement(self) -> Generator[sqlite3.Connection]:
        """Yield this thread's connection for one standalone call."""
        conn = self.conn()
        if conn.in_transaction:
            with self._unit(conn):
                yield conn
            return
        yield conn

    @contextmanager
    def _unit(self, conn: sqlite3.Connection) -> Generator[None]:
        """One operation inside an already-open transaction."""
        before = conn.total_changes
        conn.execute("SAVEPOINT unit")
        try:
            yield
        except BaseException:
            # Same here, and for the same reason: whatever the caller
            # is about to catch is more use than a failure to unwind.
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK TO unit")
                conn.execute("RELEASE unit")
            raise
        conn.execute("RELEASE unit")
        if conn.total_changes == before:
            # A read.  Counting reads made "commit every N writes" mean
            # "every N calls", so a second thread merely listing a
            # folder could end a sync's transaction mid-folder.
            return
        writes = cast(int, getattr(self._local, "writes", 0)) + 1
        if writes < self._commit_every:
            self._local.writes = writes
            return
        conn.execute("COMMIT")
        conn.execute(self._begin)
        self._local.writes = 0

    def close(self) -> None:
        """Release the connections, leaving a live batch to its owner.

        Closing a connection another thread is working through raises
        ``ProgrammingError`` *there*, mid-write, so a connection with a
        transaction open is left for the thread that owns it: its own
        block commits it, and a later ``close()`` reaps the handle.
        Everything idle is closed, and the generation bump makes every
        thread open a fresh connection rather than reach for one this
        call has shut.
        """
        with self._connections_lock:
            self._generation += 1
            busy: list[sqlite3.Connection] = []
            for conn in self._connections:
                if conn.in_transaction:
                    busy.append(conn)
                    continue
                with contextlib.suppress(sqlite3.Error):
                    conn.close()
            self._connections = busy
