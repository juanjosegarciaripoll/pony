"""Answering an invitation, with no screen and no socket.

Filing the event and telling the organizer are two operations the user
experiences as one decision, and both used to live inside the reader.
They are :mod:`pony.invitation_actions` now, so the browser can answer
an invitation the same way the terminal does — and so the states that
differ (filed but unanswered, filed and answered, filed and the answer
bounced) can be checked without driving a dialog.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

from tui_helpers import (
    make_calendar_runtime,
    make_test_account,
    make_test_config,
    make_tmp_paths,
)

from chronos.domain import CalendarRef
from chronos.outcome import Notice
from pony import invitation_actions
from pony.invitation import parse_invitation
from pony.invitation_actions import InvitationChoice

if TYPE_CHECKING:
    from pony.calendar import CalendarRuntime
    from pony.invitation import Invitation

_ACCOUNT = "personal"
_CALENDAR = "work"
TARGET = CalendarRef(_ACCOUNT, _CALENDAR)


def _ics(*, method: str = "REQUEST", sequence: int = 0) -> bytes:
    start = datetime.now(UTC) + timedelta(days=2)
    end = start + timedelta(hours=1)
    return (
        "\r\n".join(
            [
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "PRODID:-//Example//EN",
                f"METHOD:{method}",
                "BEGIN:VEVENT",
                "UID:design-review@example.com",
                f"DTSTAMP:{start.strftime('%Y%m%dT%H%M%SZ')}",
                f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}",
                f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}",
                "SUMMARY:Design review",
                "LOCATION:Room 3",
                f"SEQUENCE:{sequence}",
                "ORGANIZER:mailto:bob@example.com",
                "ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:acct@example.com",
                "END:VEVENT",
                "END:VCALENDAR",
            ]
        )
        + "\r\n"
    ).encode()


def _invitation(*, method: str = "REQUEST", sequence: int = 0) -> Invitation:
    found = parse_invitation(_ics(method=method, sequence=sequence))
    assert found is not None
    return found


def _calendar(label: str) -> CalendarRuntime:
    """A runtime whose mirror holds one (empty) calendar to file into."""
    paths = make_tmp_paths(label)
    runtime = make_calendar_runtime(
        paths,
        account_name=_ACCOUNT,
        calendar_name=_CALENDAR,
    )
    (runtime.mirror.root / _ACCOUNT / _CALENDAR).mkdir(parents=True, exist_ok=True)
    return runtime


def _config(*, can_send: bool = True):  # noqa: ANN202 - an AppConfig
    paths = make_tmp_paths("invitation-config")
    account = make_test_account(paths)
    return make_test_config(accounts=(account,) if can_send else ())


# ----------------------------------------------------------------------
# Filing
# ----------------------------------------------------------------------


def test_filing_an_invitation_puts_the_event_in_the_chosen_calendar() -> None:
    calendar = _calendar("file-invitation")

    outcome = invitation_actions.file_invitation(
        _invitation(),
        calendar=calendar,
        target=TARGET,
    )

    assert outcome.changed == 1
    assert outcome.notices[0].text == "Filed in work."
    stored = list(calendar.index.list_components_by_uid("design-review@example.com"))
    assert [component.summary for component in stored] == ["Design review"]


def test_filing_a_correction_replaces_the_event_rather_than_duplicating_it() -> None:
    calendar = _calendar("file-invitation-twice")

    invitation_actions.file_invitation(
        _invitation(),
        calendar=calendar,
        target=TARGET,
    )
    invitation_actions.file_invitation(
        _invitation(sequence=1),
        calendar=calendar,
        target=TARGET,
    )

    stored = list(calendar.index.list_components_by_uid("design-review@example.com"))
    assert len(stored) == 1


def test_a_cancellation_for_an_unknown_event_says_there_was_nothing_to_cancel() -> None:
    calendar = _calendar("file-cancellation-unknown")

    outcome = invitation_actions.file_invitation(
        _invitation(method="CANCEL"),
        calendar=calendar,
        target=TARGET,
    )

    assert outcome.changed == 0
    assert "Nothing to cancel" in outcome.notices[0].text


def test_malformed_calendar_data_is_reported_not_raised() -> None:
    import dataclasses

    calendar = _calendar("file-invitation-broken")
    broken = dataclasses.replace(_invitation(), raw_ics=b"BEGIN:VCALENDAR\r\nnope")

    outcome = invitation_actions.file_invitation(
        broken,
        calendar=calendar,
        target=TARGET,
    )

    assert outcome.failed
    assert "Could not file the invitation" in outcome.notices[0].text


# ----------------------------------------------------------------------
# Answering the organizer
# ----------------------------------------------------------------------


def test_replying_mails_the_organizer_an_itip_reply() -> None:
    config = _config()
    sent = Mock()

    with patch("pony.smtp_sender.send_message", sent):
        outcome = invitation_actions.reply_to_invitation(
            _invitation(),
            partstat="ACCEPTED",
            account=config.accounts[0],
            config=config,
            credentials=None,
        )

    assert outcome.changed == 1
    assert outcome.notices[0].text == "Accepted; the organizer was told."
    message = sent.call_args.kwargs["msg"]
    assert message["To"] == "bob@example.com"
    assert message["Subject"] == "Accepted: Design review"
    methods = [
        part.get_param("method")
        for part in message.walk()
        if part.get_content_type() == "text/calendar"
    ]
    assert "REPLY" in methods


def test_a_reply_that_cannot_be_sent_says_the_event_was_still_filed() -> None:
    from pony.smtp_sender import SMTPError

    config = _config()

    def _refuse(**_kwargs: object) -> None:
        raise SMTPError("the server hung up")

    with patch("pony.smtp_sender.send_message", _refuse):
        outcome = invitation_actions.reply_to_invitation(
            _invitation(),
            partstat="DECLINED",
            account=config.accounts[0],
            config=config,
            credentials=None,
        )

    assert outcome.failed
    assert "Filed, but the reply could not be sent" in outcome.notices[0].text


def test_an_invitation_with_no_organizer_has_nobody_to_answer() -> None:
    import dataclasses

    config = _config()
    anonymous = dataclasses.replace(_invitation(), organizer=None)

    outcome = invitation_actions.reply_to_invitation(
        anonymous,
        partstat="ACCEPTED",
        account=config.accounts[0],
        config=config,
        credentials=None,
    )

    assert outcome.notices[0].text == "No organizer to reply to."


# ----------------------------------------------------------------------
# Both halves at once, which is what the browser calls
# ----------------------------------------------------------------------


def test_responding_files_the_event_and_then_answers() -> None:
    calendar = _calendar("respond-accept")
    config = _config()
    sent = Mock()

    with patch("pony.smtp_sender.send_message", sent):
        outcome = invitation_actions.respond_to_invitation(
            _invitation(),
            InvitationChoice(calendar=TARGET, partstat="ACCEPTED"),
            calendar=calendar,
            account=config.accounts[0],
            config=config,
            credentials=None,
        )

    assert [notice.text for notice in outcome.notices] == [
        "Filed in work.",
        "Accepted; the organizer was told.",
    ]
    assert sent.called


def test_responding_without_a_partstat_tells_nobody() -> None:
    """Filing an announcement is not the same as answering it."""
    calendar = _calendar("respond-file-only")
    config = _config()
    sent = Mock()

    with patch("pony.smtp_sender.send_message", sent):
        outcome = invitation_actions.respond_to_invitation(
            _invitation(),
            InvitationChoice(calendar=TARGET, partstat=None),
            calendar=calendar,
            account=config.accounts[0],
            config=config,
            credentials=None,
        )

    assert [notice.text for notice in outcome.notices] == ["Filed in work."]
    assert not sent.called


def test_responding_does_not_answer_when_filing_failed() -> None:
    """There is no point telling an organizer about a refused calendar."""
    import dataclasses

    calendar = _calendar("respond-broken")
    config = _config()
    sent = Mock()
    broken = dataclasses.replace(_invitation(), raw_ics=b"BEGIN:VCALENDAR\r\nnope")

    with patch("pony.smtp_sender.send_message", sent):
        outcome = invitation_actions.respond_to_invitation(
            broken,
            InvitationChoice(calendar=TARGET, partstat="ACCEPTED"),
            calendar=calendar,
            account=config.accounts[0],
            config=config,
            credentials=None,
        )

    assert outcome.failed
    assert not sent.called


def test_responding_with_no_sending_account_says_nothing_was_answered() -> None:
    calendar = _calendar("respond-no-account")
    config = _config()

    outcome = invitation_actions.respond_to_invitation(
        _invitation(),
        InvitationChoice(calendar=TARGET, partstat="ACCEPTED"),
        calendar=calendar,
        account=None,
        config=config,
        credentials=None,
    )

    assert outcome.notices[-1].text == (
        "No account configured for sending — nothing was answered."
    )


# ----------------------------------------------------------------------
# Which identity answers
# ----------------------------------------------------------------------


def test_the_reply_comes_from_the_account_the_invitation_arrived_on() -> None:
    paths = make_tmp_paths("replying-account")
    first = make_test_account(paths, name="first")
    second = make_test_account(paths, name="second")
    config = make_test_config(accounts=(first, second))

    chosen = invitation_actions.replying_account(config, arrived_on="second")

    assert not isinstance(chosen, Notice)
    assert chosen.name == "second"


def test_an_unknown_arrival_account_falls_back_to_the_first_that_can_send() -> None:
    config = _config()

    chosen = invitation_actions.replying_account(config, arrived_on="deleted")

    assert not isinstance(chosen, Notice)
    assert chosen.name == "acct"


def test_no_sendable_account_is_reported_rather_than_guessed() -> None:
    refused = invitation_actions.replying_account(
        _config(can_send=False),
        arrived_on=None,
    )

    assert isinstance(refused, Notice)
    assert refused.text == "No account configured for sending."


def test_an_invitation_with_nobody_to_answer_is_only_filed() -> None:
    """`can_reply` is checked by the action, not only by the buttons."""
    import dataclasses

    from pony.invitation_actions import InvitationChoice, respond_to_invitation

    runtime = _calendar("inv-no-organizer")
    invitation = dataclasses.replace(_invitation(), organizer=None)
    assert not invitation.can_reply

    outcome = respond_to_invitation(
        invitation,
        InvitationChoice(calendar=TARGET, partstat="ACCEPTED"),
        calendar=runtime,
        account=None,
        config=make_test_config(accounts=()),
        credentials=None,
    )

    # Filed, and nothing said about an answer that cannot be sent.
    assert outcome.changed == 1
    assert not any("answered" in notice.text for notice in outcome.notices)
