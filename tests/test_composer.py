"""Tests for pony.composer.

The composer UI asks for a DraftSpec and renders it; how a reply is
assembled — which identity sends it, how the subject is prefixed, who
survives a reply-all, how it threads — is decided here. These were
previously inlined at five separate entry points.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from uuid import uuid4

from conftest import TMP_ROOT

from pony.compose_utils import build_email_message
from pony.composer import (
    DraftSpec,
    forward_attachment_name,
    forward_draft,
    markdown_default,
    new_draft,
    preferred_account,
    reply_draft,
    resumed_draft,
)
from pony.domain import (
    AccountConfig,
    AnyAccount,
    AppConfig,
    MirrorConfig,
    SmtpConfig,
)
from pony.message_renderer import RenderedMessage


def _account(
    name: str = "work",
    *,
    signature: str | None = None,
    markdown: bool = False,
) -> AccountConfig:
    path = TMP_ROOT / "composer-tests" / uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return AccountConfig(
        name=name,
        email_address=f"{name}@example.com",
        imap_host="imap.example.com",
        smtp=SmtpConfig(host="smtp.example.com"),
        username=name,
        credentials_source="plaintext",
        mirror=MirrorConfig(path=path, format="maildir"),
        password="pw",
        signature=signature,
        markdown_compose=markdown,
    )


def _config(markdown: bool = False) -> AppConfig:
    return AppConfig(accounts=(), markdown_compose=markdown)


def _rendered(
    *,
    subject: str = "Tuesday meeting",
    from_: str = "Alice <alice@example.com>",
    to: str = "work@example.com, Carol <carol@example.com>",
    cc: str = "",
    message_id: str = "<parent@example.com>",
    references: str = "",
) -> RenderedMessage:
    # build_reply_all_recipients re-parses the raw headers (that is what
    # keeps a quoted comma in a display name from splitting an address),
    # so the fixture has to carry real ones.
    lines = [f"From: {from_}", f"To: {to}"]
    if cc:
        lines.append(f"Cc: {cc}")
    lines += [f"Subject: {subject}", "", "Original body."]
    return RenderedMessage(
        subject=subject,
        from_=from_,
        to=to,
        cc=cc,
        date="Fri, 17 Apr 2026 12:00:00 +0000",
        body="Original body.",
        attachments=(),
        raw_bytes="\r\n".join(lines).encode(),
        message_id=message_id,
        references=references,
    )


class PreferredAccountTest(unittest.TestCase):
    def test_the_preferred_account_is_used_when_it_can_send(self) -> None:
        accounts: list[AnyAccount] = [_account("a"), _account("b")]
        self.assertEqual(preferred_account(accounts, prefer_name="b").name, "b")

    def test_an_unknown_preference_falls_back_to_the_first(self) -> None:
        # The message being replied to may belong to a local account,
        # which cannot send; the dropdown still needs a valid default.
        accounts: list[AnyAccount] = [_account("a"), _account("b")]
        self.assertEqual(preferred_account(accounts, prefer_name="local").name, "a")

    def test_no_preference_falls_back_to_the_first(self) -> None:
        accounts: list[AnyAccount] = [_account("a"), _account("b")]
        self.assertEqual(preferred_account(accounts, prefer_name=None).name, "a")


class MarkdownDefaultTest(unittest.TestCase):
    def test_the_account_setting_wins(self) -> None:
        self.assertTrue(markdown_default(_account(markdown=True), _config(False)))

    def test_the_global_setting_applies_without_an_account_setting(self) -> None:
        self.assertTrue(markdown_default(_account(markdown=False), _config(True)))

    def test_off_when_neither_is_set(self) -> None:
        self.assertFalse(markdown_default(_account(markdown=False), _config(False)))


class NewDraftTest(unittest.TestCase):
    def test_it_is_empty_apart_from_the_signature(self) -> None:
        spec = new_draft(accounts=[_account(signature="-- \nAlice")], config=_config())
        self.assertEqual(spec.to, "")
        self.assertEqual(spec.subject, "")
        self.assertIn("Alice", spec.body)

    def test_a_recipient_can_be_supplied(self) -> None:
        spec = new_draft(accounts=[_account()], config=_config(), to="bob@example.com")
        self.assertEqual(spec.to, "bob@example.com")

    def test_a_fresh_message_starts_its_own_thread(self) -> None:
        spec = new_draft(accounts=[_account()], config=_config())
        self.assertEqual(spec.in_reply_to, "")
        self.assertEqual(spec.references, "")


class ReplyDraftTest(unittest.TestCase):
    def test_a_reply_goes_only_to_the_sender(self) -> None:
        spec = reply_draft(
            accounts=[_account()], config=_config(), rendered=_rendered()
        )
        self.assertEqual(spec.to, "Alice <alice@example.com>")
        self.assertEqual(spec.cc, "")

    def test_a_reply_all_keeps_the_other_recipients(self) -> None:
        spec = reply_draft(
            accounts=[_account()],
            config=_config(),
            rendered=_rendered(),
            reply_all=True,
        )
        self.assertEqual(spec.to, "Alice <alice@example.com>")
        self.assertIn("carol@example.com", spec.cc)

    def test_a_reply_all_drops_the_sending_identity(self) -> None:
        spec = reply_draft(
            accounts=[_account("work")],
            config=_config(),
            rendered=_rendered(),
            reply_all=True,
        )
        self.assertNotIn("work@example.com", spec.cc)

    def test_both_reply_forms_thread_identically(self) -> None:
        # The whole reason reply and reply-all share one function: any
        # rule added to one used to be missing from the other.
        rendered = _rendered(references="<root@example.com>")
        one = reply_draft(accounts=[_account()], config=_config(), rendered=rendered)
        allof = reply_draft(
            accounts=[_account()],
            config=_config(),
            rendered=rendered,
            reply_all=True,
        )
        self.assertEqual(one.in_reply_to, "<parent@example.com>")
        self.assertEqual(one.in_reply_to, allof.in_reply_to)
        self.assertEqual(one.references, allof.references)
        self.assertEqual(one.references, "<root@example.com> <parent@example.com>")
        self.assertEqual(one.subject, allof.subject)

    def test_the_subject_is_prefixed_once(self) -> None:
        spec = reply_draft(
            accounts=[_account()],
            config=_config(),
            rendered=_rendered(subject="Re: Tuesday meeting"),
        )
        self.assertEqual(spec.subject, "Re: Tuesday meeting")

    def test_the_body_quotes_the_original(self) -> None:
        spec = reply_draft(
            accounts=[_account()], config=_config(), rendered=_rendered()
        )
        self.assertIn("> Original body.", spec.body)


class ForwardDraftTest(unittest.TestCase):
    def test_a_forward_starts_its_own_thread(self) -> None:
        spec = forward_draft(
            accounts=[_account()],
            config=_config(),
            rendered=_rendered(),
            attachment=Path("/tmp/x.eml"),
        )
        self.assertEqual(spec.in_reply_to, "")
        self.assertEqual(spec.references, "")

    def test_it_has_no_recipient_yet(self) -> None:
        spec = forward_draft(
            accounts=[_account()],
            config=_config(),
            rendered=_rendered(),
            attachment=Path("/tmp/x.eml"),
        )
        self.assertEqual(spec.to, "")
        self.assertEqual(spec.subject, "Fwd: Tuesday meeting")

    def test_the_composer_owns_the_materialised_copy(self) -> None:
        # Listed in both: attached to the message, and owned so the
        # composer deletes it on close.
        attachment = Path("/tmp/forwarded.eml")
        spec = forward_draft(
            accounts=[_account()],
            config=_config(),
            rendered=_rendered(),
            attachment=attachment,
        )
        self.assertEqual(spec.attachment_paths, (attachment,))
        self.assertEqual(spec.owned_paths, (attachment,))

    def test_the_attachment_is_named_after_the_subject(self) -> None:
        self.assertEqual(
            forward_attachment_name("Tuesday meeting"), "Tuesday meeting.eml"
        )

    def test_a_hostile_subject_cannot_steer_the_path(self) -> None:
        name = forward_attachment_name("../../.bashrc")
        self.assertNotIn("/", name)
        self.assertNotIn("..", name.replace(".eml", "").replace(".bashrc", ""))

    def test_a_blank_subject_still_yields_a_usable_name(self) -> None:
        self.assertEqual(forward_attachment_name("   "), "forwarded message.eml")


class ResumedDraftTest(unittest.TestCase):
    def test_the_fields_come_back(self) -> None:
        raw = build_email_message(
            from_address="work@example.com",
            to="bob@example.com",
            cc="carol@example.com",
            bcc="dan@example.com",
            subject="Half written",
            body="so far",
            attachment_paths=[],
        ).as_bytes()
        spec = resumed_draft(accounts=[_account()], config=_config(), raw=raw)
        self.assertEqual(spec.to, "bob@example.com")
        self.assertEqual(spec.cc, "carol@example.com")
        self.assertEqual(spec.bcc, "dan@example.com")
        self.assertEqual(spec.subject, "Half written")
        self.assertIn("so far", spec.body)

    def test_a_resumed_reply_stays_in_its_thread(self) -> None:
        raw = build_email_message(
            from_address="work@example.com",
            to="bob@example.com",
            cc="",
            bcc="",
            subject="Re: Hello",
            body="half",
            attachment_paths=[],
            in_reply_to="<parent@example.com>",
            references="<root@example.com> <parent@example.com>",
        ).as_bytes()
        spec = resumed_draft(accounts=[_account()], config=_config(), raw=raw)
        self.assertEqual(spec.in_reply_to, "<parent@example.com>")
        self.assertEqual(spec.references, "<root@example.com> <parent@example.com>")


class DraftSpecTest(unittest.TestCase):
    def test_it_is_immutable(self) -> None:
        # The composer renders it; it must not drift underneath.
        spec = DraftSpec(account_name="work")
        with self.assertRaises(AttributeError):
            spec.to = "someone@example.com"  # type: ignore[misc]
