"""Build a throwaway Pony Express data set for documentation screenshots.

This module fabricates a self-contained mail store — synthetic accounts,
messages, and contacts — using only Pony's public repositories.  It never
contacts a network or touches a real account; everything lives under a
caller-supplied temp directory.  ``scripts/capture_screenshots.py`` drives a
headless Textual session over the result and exports PNG stills.

Run directly to dump a store somewhere and poke at it:

    uv run python scripts/demo_seed.py /tmp/pony-demo
"""

from __future__ import annotations

import dataclasses
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

from pony.credentials import PlaintextCredentialsProvider
from pony.domain import (
    AccountConfig,
    AppConfig,
    Contact,
    FolderRef,
    MessageFlag,
    MessageRef,
    MirrorConfig,
    SmtpConfig,
)
from pony.index_store import SqliteIndexRepository
from pony.message_projection import project_rfc822_message
from pony.paths import AppPaths
from pony.protocols import CredentialsProvider
from pony.storage import MaildirMirrorRepository

# A fixed clock so regenerated screenshots are byte-stable from run to run.
NOW = datetime(2026, 6, 23, 17, 30, tzinfo=UTC)
OWNER = "you@ponyexpress.dev"


@dataclass(frozen=True)
class DemoData:
    """Everything the capture script needs to build the Textual apps."""

    config: AppConfig
    account: AccountConfig
    index: SqliteIndexRepository
    mirrors: dict[str, MaildirMirrorRepository]
    credentials: CredentialsProvider
    paths: AppPaths


@dataclass(frozen=True)
class _Msg:
    sender: str
    subject: str
    body: str
    age_minutes: int
    flags: tuple[MessageFlag, ...] = ()
    attachment: str | None = None
    to: str = OWNER


# A plausible developer inbox.  Senders double as harvested contacts below.
_INBOX: tuple[_Msg, ...] = (
    _Msg(
        sender="Grace Hopper <grace.hopper@navy.mil>",
        subject="Re: Compiler review notes",
        body=(
            "Went through your review comments — the nanosecond demo lands "
            "well. One nit on the parser pass, otherwise ship it.\n\nGrace"
        ),
        age_minutes=18,
        flags=(MessageFlag.FLAGGED,),
    ),
    _Msg(
        sender="Katherine Johnson <k.johnson@nasa.gov>",
        subject="Trajectory figures for the review",
        body=(
            "Attaching the re-checked figures. The re-entry corridor numbers "
            "match the hand calculations to four decimals.\n\nKatherine"
        ),
        age_minutes=52,
        attachment="trajectory.pdf",
    ),
    _Msg(
        sender="Alan Turing <alan@bletchley.uk>",
        subject="Enigma settings for Thursday",
        body=(
            "The new rotor order is ready for Thursday's run. Bombe time is "
            "booked from 09:00. Bring the crib sheets.\n\nAlan"
        ),
        age_minutes=140,
    ),
    _Msg(
        sender="Donald Knuth <knuth@stanford.edu>",
        subject="Volume 4B — your feedback",
        body=(
            "Thank you for the careful read. I have enclosed a reward cheque "
            "for the two genuine errors you found.\n\nDEK"
        ),
        age_minutes=190,
        flags=(MessageFlag.FLAGGED,),
    ),
    _Msg(
        sender="Radia Perlman <radia@spanning.tree>",
        subject="Spanning-tree meetup?",
        body=(
            "A few of us are gathering next week to argue about loops in "
            "bridged networks. You in?\n\nRadia"
        ),
        age_minutes=320,
    ),
    _Msg(
        sender="Barbara Liskov <liskov@mit.edu>",
        subject="Re: Substitution principle talk",
        body=(
            "Slides look good. Let's keep the abstraction examples and drop "
            "the last benchmark.\n\nBarbara"
        ),
        age_minutes=1180,
        flags=(MessageFlag.SEEN, MessageFlag.ANSWERED),
    ),
    _Msg(
        sender="Linus Torvalds <torvalds@kernel.org>",
        subject="Re: [PATCH] mailmap update",
        body=(
            "Applied to my tree. Please keep the commit message under the "
            "line limit next time.\n\nLinus"
        ),
        age_minutes=1490,
        flags=(MessageFlag.SEEN,),
    ),
    _Msg(
        sender="Margaret Hamilton <mhamilton@mit.edu>",
        subject="Apollo guidance review checklist",
        body=(
            "Here is the priority-display checklist for the review. Every "
            "restart path is covered now.\n\nMargaret"
        ),
        age_minutes=1600,
        flags=(MessageFlag.SEEN,),
    ),
    _Msg(
        sender="Python Weekly <news@pythonweekly.dev>",
        subject="Python Weekly — Issue 642",
        body=(
            "This week: structural pattern matching in the wild, a tour of "
            "the new REPL, and five terminal UI libraries to watch."
        ),
        age_minutes=2300,
    ),
    _Msg(
        sender="Vint Cerf <vint@arpa.net>",
        subject="Re: Internet draft comments",
        body=(
            "The congestion-control section reads cleanly now. Ready for "
            "last call as far as I'm concerned.\n\nVint"
        ),
        age_minutes=2880,
        flags=(MessageFlag.SEEN,),
    ),
    _Msg(
        sender="Dennis Ritchie <dmr@belllabs.com>",
        subject="K&R, third edition?",
        body=(
            "People keep asking. I keep saying the second edition is fine. "
            "What do you think?\n\ndmr"
        ),
        age_minutes=4320,
        flags=(MessageFlag.SEEN,),
    ),
    _Msg(
        sender="Pony CI <ci@ponyexpress.dev>",
        subject="Build passed on main",
        body="All 931 tests green. Coverage 85.1%. Artifacts attached to the run.",
        age_minutes=5760,
        flags=(MessageFlag.SEEN,),
    ),
)

_SENT: tuple[_Msg, ...] = (
    _Msg(
        sender=f"You <{OWNER}>",
        to="Grace Hopper <grace.hopper@navy.mil>",
        subject="Compiler review notes",
        body="Left comments inline. The nanosecond visual is a keeper.",
        age_minutes=30,
        flags=(MessageFlag.SEEN,),
    ),
    _Msg(
        sender=f"You <{OWNER}>",
        to="Barbara Liskov <liskov@mit.edu>",
        subject="Substitution principle talk",
        body="First cut of the slides attached. Feedback welcome.",
        age_minutes=1200,
        flags=(MessageFlag.SEEN,),
    ),
)

_ARCHIVE: tuple[_Msg, ...] = (
    _Msg(
        sender="Ada Lovelace <ada@analytical.engine>",
        subject="Note G, annotated",
        body="The Bernoulli routine, with the loop unrolled for clarity.",
        age_minutes=20160,
        flags=(MessageFlag.SEEN,),
    ),
    _Msg(
        sender="Edsger Dijkstra <ewd@tue.nl>",
        subject="On the shortest path",
        body="A short note. Goto considered, as ever, harmful.",
        age_minutes=43200,
        flags=(MessageFlag.SEEN,),
    ),
)

_LIST: tuple[_Msg, ...] = (
    _Msg(
        sender="Will McGugan <will@textualize.io>",
        subject="[textual] 8.2 released",
        body="Smoother scrolling, faster table rendering, and new themes.",
        age_minutes=2640,
    ),
    _Msg(
        sender="textual-dev <list@textualize.io>",
        subject="[textual] Snapshot testing tips",
        body="A thread on driving apps headlessly with the Pilot test driver.",
        age_minutes=8640,
        flags=(MessageFlag.SEEN,),
    ),
)

_CONTACTS: tuple[Contact, ...] = (
    Contact(
        id=None,
        first_name="Grace",
        last_name="Hopper",
        emails=("grace.hopper@navy.mil",),
        organization="US Navy",
        affix=("Rear Admiral",),
        notes="Coined the term 'debugging'. Keeps a nanosecond on her desk.",
        message_count=42,
        last_seen=NOW - timedelta(minutes=18),
    ),
    Contact(
        id=None,
        first_name="Katherine",
        last_name="Johnson",
        emails=("k.johnson@nasa.gov",),
        organization="NASA Langley",
        notes="Orbital mechanics. Checks the computers' work by hand.",
        message_count=17,
        last_seen=NOW - timedelta(minutes=52),
    ),
    Contact(
        id=None,
        first_name="Alan",
        last_name="Turing",
        emails=("alan@bletchley.uk",),
        organization="Bletchley Park",
        message_count=23,
        last_seen=NOW - timedelta(minutes=140),
    ),
    Contact(
        id=None,
        first_name="Barbara",
        last_name="Liskov",
        emails=("liskov@mit.edu",),
        organization="MIT CSAIL",
        aliases=("barbara",),
        message_count=9,
        last_seen=NOW - timedelta(minutes=1180),
    ),
    Contact(
        id=None,
        first_name="Margaret",
        last_name="Hamilton",
        emails=("mhamilton@mit.edu",),
        organization="MIT Draper Lab",
        notes="Coined 'software engineering'.",
        message_count=11,
        last_seen=NOW - timedelta(minutes=1600),
    ),
    Contact(
        id=None,
        first_name="Donald",
        last_name="Knuth",
        emails=("knuth@stanford.edu",),
        organization="Stanford",
        affix=("Prof.",),
        message_count=6,
        last_seen=NOW - timedelta(minutes=190),
    ),
    Contact(
        id=None,
        first_name="Radia",
        last_name="Perlman",
        emails=("radia@spanning.tree",),
        organization="Network Working Group",
        message_count=8,
        last_seen=NOW - timedelta(minutes=320),
    ),
    Contact(
        id=None,
        first_name="Linus",
        last_name="Torvalds",
        emails=("torvalds@kernel.org",),
        organization="Linux Foundation",
        message_count=31,
        last_seen=NOW - timedelta(minutes=1490),
    ),
    Contact(
        id=None,
        first_name="Vint",
        last_name="Cerf",
        emails=("vint@arpa.net",),
        organization="Internet pioneers",
        message_count=14,
        last_seen=NOW - timedelta(minutes=2880),
    ),
    Contact(
        id=None,
        first_name="Ada",
        last_name="Lovelace",
        emails=("ada@analytical.engine",),
        organization="Analytical Engine",
        notes="First programmer.",
        message_count=4,
        last_seen=NOW - timedelta(minutes=20160),
    ),
)


def _raw(msg: _Msg) -> bytes:
    """Render one ``_Msg`` to RFC 5322 bytes."""
    mail = EmailMessage()
    mail["From"] = msg.sender
    mail["To"] = msg.to
    mail["Subject"] = msg.subject
    mail["Date"] = format_datetime(NOW - timedelta(minutes=msg.age_minutes))
    mail["Message-ID"] = f"<{abs(hash((msg.sender, msg.subject)))}@ponyexpress.dev>"
    mail.set_content(msg.body)
    if msg.attachment is not None:
        # Pad to a plausible document size so the UI shows e.g. "18 KB".
        payload = b"%PDF-1.4\n% demo trajectory figures\n" + b"\x00" * 18_000
        mail.add_attachment(
            payload,
            maintype="application",
            subtype="pdf",
            filename=msg.attachment,
        )
    return mail.as_bytes()


def _seed_folder(
    *,
    index: SqliteIndexRepository,
    mirror: MaildirMirrorRepository,
    folder: FolderRef,
    messages: tuple[_Msg, ...],
) -> None:
    for msg in messages:
        raw = _raw(msg)
        storage_key = mirror.store_message(folder=folder, raw_message=raw)
        ref = MessageRef(
            account_name=folder.account_name,
            folder_name=folder.folder_name,
            id=0,
        )
        projected = project_rfc822_message(
            message_ref=ref,
            raw_message=raw,
            storage_key=storage_key,
        )
        projected = dataclasses.replace(
            projected,
            local_flags=frozenset(msg.flags),
            base_flags=frozenset(msg.flags),
        )
        index.insert_message(message=projected)


def build_demo(root: Path) -> DemoData:
    """Create a fully seeded demo store under *root* and return its handles."""
    root.mkdir(parents=True, exist_ok=True)
    data_dir = root / "data"
    paths = AppPaths(
        config_file=root / "config.toml",
        data_dir=data_dir,
        state_dir=root / "state",
        cache_dir=root / "cache",
        log_dir=root / "state" / "logs",
        index_db_file=data_dir / "index.sqlite3",
    )
    mirror_dir = data_dir / "mirrors" / "personal"
    mirror_dir.mkdir(parents=True, exist_ok=True)
    account = AccountConfig(
        name="Personal",
        full_name="You",
        email_address=OWNER,
        imap_host="imap.ponyexpress.dev",
        smtp=SmtpConfig(host="smtp.ponyexpress.dev"),
        username="you",
        credentials_source="plaintext",
        mirror=MirrorConfig(path=mirror_dir, format="maildir"),
        password="demo",
        archive_folder="Archive",
    )
    config = AppConfig(accounts=(account,))

    paths.index_db_file.parent.mkdir(parents=True, exist_ok=True)
    index = SqliteIndexRepository(database_path=paths.index_db_file)
    index.initialize()
    mirror = MaildirMirrorRepository(
        account_name=account.name,
        root_dir=account.mirror.path,
    )

    acct = account.name
    _seed_folder(
        index=index,
        mirror=mirror,
        folder=FolderRef(account_name=acct, folder_name="INBOX"),
        messages=_INBOX,
    )
    _seed_folder(
        index=index,
        mirror=mirror,
        folder=FolderRef(account_name=acct, folder_name="Sent"),
        messages=_SENT,
    )
    _seed_folder(
        index=index,
        mirror=mirror,
        folder=FolderRef(account_name=acct, folder_name="Archive"),
        messages=_ARCHIVE,
    )
    _seed_folder(
        index=index,
        mirror=mirror,
        folder=FolderRef(account_name=acct, folder_name="Lists.Textual"),
        messages=_LIST,
    )

    for contact in _CONTACTS:
        index.upsert_contact(contact=contact)

    credentials = PlaintextCredentialsProvider(config)
    return DemoData(
        config=config,
        account=account,
        index=index,
        mirrors={account.name: mirror},
        credentials=credentials,
        paths=paths,
    )


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/pony-demo")
    demo = build_demo(target)
    inbox = demo.index.list_folder_message_summaries(
        folder=FolderRef(account_name=demo.account.name, folder_name="INBOX")
    )
    print(f"Seeded demo store at {target}")
    print(f"  INBOX: {len(inbox)} messages")
    print(f"  contacts: {len(demo.index.list_all_contacts())}")
