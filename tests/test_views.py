"""The mail reader's row projections, with no widget involved.

These used to be module-level privates inside the message-list widget,
reachable only by importing it — which meant importing Textual to ask
what marker a flagged message gets.  They live in :mod:`pony.mail.views` now.

``format_received`` takes *now* rather than reading the clock, so these
pin both ends of the comparison instead of guarding against New Year's
Day the way the old date tests had to.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

from corpus import plain_text
from tui_helpers import (
    make_index,
    make_mirrors,
    make_test_account,
    make_test_config,
    make_tmp_paths,
    seed_message,
)

from pony.mail.domain import (
    Contact,
    FolderMessageSummary,
    FolderRef,
    MessageFlag,
    MessageRef,
    MessageStatus,
)
from pony.mail.views import (
    ANSWERED_MARK,
    ATTACHMENT_MARK,
    FLAGGED_MARK,
    MARKED_MARK,
    NO_MARK,
    contact_row,
    format_received,
    message_row,
    status_mark,
    summary_of,
)

NOW = datetime(2026, 4, 17, 15, 30, tzinfo=UTC)

_SUMMARY = FolderMessageSummary(
    message_ref=MessageRef(account_name="acct", folder_name="INBOX", id=7),
    message_id="<row@example.com>",
    storage_key="key",
    sender="Sender <sender@example.com>",
    recipients="me@example.com",
    subject="Row probe",
    received_at=datetime(2026, 4, 17, 12, tzinfo=UTC),
    has_attachments=False,
    local_flags=frozenset(),
    local_status=MessageStatus.ACTIVE,
)


def _summary(**overrides: object) -> FolderMessageSummary:
    return dataclasses.replace(_SUMMARY, **overrides)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# Status markers
# ----------------------------------------------------------------------


def test_status_marks_rank_flagged_over_answered_over_attachments() -> None:
    flagged_and_answered = _summary(
        local_flags=frozenset({MessageFlag.FLAGGED, MessageFlag.ANSWERED}),
        has_attachments=True,
    )

    assert status_mark(flagged_and_answered) == FLAGGED_MARK
    assert (
        status_mark(
            _summary(
                local_flags=frozenset({MessageFlag.ANSWERED}),
                has_attachments=True,
            ),
        )
        == ANSWERED_MARK
    )
    assert status_mark(_summary(has_attachments=True)) == ATTACHMENT_MARK
    assert status_mark(_summary()) == NO_MARK


def test_a_marked_row_says_so_instead_of_its_status() -> None:
    """A row marked for an action has the more urgent thing to say."""
    summary = _summary(local_flags=frozenset({MessageFlag.FLAGGED}))

    assert message_row(summary, show_recipients=False, marked=True).icon == MARKED_MARK
    assert message_row(summary, show_recipients=False).icon == FLAGGED_MARK


# ----------------------------------------------------------------------
# Dates
# ----------------------------------------------------------------------


def test_todays_mail_shows_only_the_time() -> None:
    received = datetime(2026, 4, 17, 9, 5, tzinfo=UTC)

    assert format_received(received, now=NOW) == "09:05"


def test_this_years_mail_shows_the_day_and_month() -> None:
    received = datetime(2026, 1, 3, 9, 5, tzinfo=UTC)

    assert format_received(received, now=NOW) == "Jan 03"


def test_older_mail_carries_its_year() -> None:
    """The year is the only thing that tells it from this April."""
    received = datetime(2001, 3, 4, 9, 30, tzinfo=UTC)

    assert format_received(received, now=NOW) == "2001-03-04"


def test_a_moment_is_rendered_in_the_readers_own_zone() -> None:
    """Stored times are UTC; a reader is not.

    At UTC+2, mail that arrived at 00:30 local was rendered as a *date*
    -- its UTC day was the day before -- while every calendar view
    renders the same moment locally (`pony.calendar.views` starts each of its
    three with `start.astimezone()`).
    """
    from datetime import timedelta, timezone

    east = timezone(timedelta(hours=2))
    # 00:30 on the 17th where this reader is sitting.
    arrived = datetime(2026, 4, 16, 22, 30, tzinfo=UTC)

    assert format_received(arrived, now=NOW.astimezone(east)) == "00:30"
    # The same moment for a reader ten hours behind: still the 16th
    # there, so the row carries the date rather than a bare clock.
    west = timezone(timedelta(hours=-10))
    assert format_received(arrived, now=NOW.astimezone(west)) == "Apr 16"


def test_format_received_reads_the_clock_when_not_told_otherwise() -> None:
    now = datetime.now(tz=UTC)

    # The reader's clock, not UTC's. This assertion used to read
    # `now.strftime(...)`, which is the defect the test was meant to
    # cover: UTC in, UTC out, and no zone anywhere in the suite.
    assert format_received(now) == now.astimezone().strftime("%H:%M")


# ----------------------------------------------------------------------
# Whole rows
# ----------------------------------------------------------------------


def test_a_row_carries_the_fields_a_renderer_lays_out() -> None:
    row = message_row(_SUMMARY, show_recipients=False, now=NOW)

    assert row.icon == NO_MARK
    assert row.date == "12:00"
    assert row.correspondent == "Sender <sender@example.com>"
    assert row.subject == "Row probe"
    # Nothing has been read, so the exception is what gets reported.
    assert row.unread


def test_a_sent_folder_row_names_the_recipient() -> None:
    """There the sender is the user on every row and tells them apart."""
    row = message_row(_SUMMARY, show_recipients=True, now=NOW)

    assert row.correspondent == "me@example.com"


def test_a_read_row_is_not_unread() -> None:
    summary = _summary(local_flags=frozenset({MessageFlag.SEEN}))

    assert not message_row(summary, show_recipients=False, now=NOW).unread


def test_a_subjectless_message_is_still_given_a_subject() -> None:
    row = message_row(_summary(subject=""), show_recipients=False, now=NOW)

    assert row.subject == "(no subject)"


# ----------------------------------------------------------------------
# Narrowing a full row
# ----------------------------------------------------------------------


def test_summary_of_keeps_what_a_list_row_needs() -> None:
    paths = make_tmp_paths("views-summary")
    account = make_test_account(paths)
    config = make_test_config(accounts=(account,))
    index = make_index(paths)
    mirrors = make_mirrors(config)
    folder = FolderRef(account_name="acct", folder_name="INBOX")
    ref = seed_message(
        index=index,
        mirror=mirrors["acct"],
        folder=folder,
        raw=plain_text(),
    )
    message = index.get_message(message_ref=ref)
    assert message is not None

    summary = summary_of(message)

    # Every field the list renders, and nothing that needs re-parsing.
    assert summary.message_ref == message.message_ref
    assert summary.sender == message.sender
    assert summary.recipients == message.recipients
    assert summary.subject == message.subject
    assert summary.received_at == message.received_at
    assert summary.local_flags == message.local_flags
    assert summary.local_status == message.local_status
    # And it is the same row the index hands out for the whole folder.
    (from_index,) = index.list_folder_message_summaries(folder=folder)
    assert summary == from_index


# ----------------------------------------------------------------------
# Contacts
# ----------------------------------------------------------------------


def test_a_contact_row_summarises_its_addresses() -> None:
    row = contact_row(
        Contact(
            id=1,
            first_name="Ada",
            last_name="Lovelace",
            emails=("a@x.test", "b@x.test", "c@x.test", "d@x.test"),
            organization="Analytical",
        ),
    )

    assert row.emails == "a@x.test, b@x.test, c@x.test (+1)"
    assert (row.last_name, row.first_name) == ("Lovelace", "Ada")
    assert row.organization == "Analytical"
    assert row.marked is False
    assert row.mark == NO_MARK


def test_a_marked_contact_says_so_as_data() -> None:
    row = contact_row(
        Contact(id=2, first_name="Grace", last_name="Hopper", emails=()),
        marked=True,
    )

    assert row.marked is True
    assert row.mark == MARKED_MARK
    assert row.emails == ""


def test_the_correspondent_column_is_named_once() -> None:
    """ "To" in a folder of sent mail, "From" everywhere else.

    Both front ends labelled the column themselves -- one in a padded
    header string, the other in a `columns(...)` call -- so the word
    could have come out different in one of them.  Which folders show
    the recipient is `folder_utils.shows_recipient`; what the column is
    called is this.
    """
    from pony.mail.views import correspondent_label

    assert correspondent_label(shows_recipient=True) == "To"
    assert correspondent_label(shows_recipient=False) == "From"
