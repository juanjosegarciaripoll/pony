"""Reading the receiving server's ``Authentication-Results`` header.

The headers here are the shapes real servers write, including the ones
that matter most: a passing signature from a domain that is *not* the
one in ``From``, and a forged report added by the sender.
"""

from __future__ import annotations

import email
import email.policy
import unittest
from email.message import EmailMessage

import corpus

from pony.mail.authentication import (
    Authenticity,
    MessageAuthentication,
    parse_authentication,
)
from pony.mail.message_renderer import build_browser_html, render_message


def _message(
    *, headers: str, from_: str = "Notifications <no@github.com>"
) -> EmailMessage:
    raw = f"From: {from_}\nTo: me@example.org\nSubject: hi\n{headers}\n\nbody\n"
    msg = email.message_from_string(raw, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)
    return msg


class VerdictTest(unittest.TestCase):
    def test_a_dmarc_pass_is_the_strongest_report(self) -> None:
        auth = parse_authentication(
            _message(
                headers="Authentication-Results: mx.example.org;\n"
                " dkim=pass header.i=@github.com header.s=pf2023;\n"
                " spf=pass (example.org: domain of bounce@github.com designates"
                " 1.2.3.4) smtp.mailfrom=bounce@github.com;\n"
                " dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=github.com"
            )
        )

        self.assertIs(auth.verdict, Authenticity.PASSED)
        self.assertEqual(auth.signed_domain, "github.com")
        self.assertEqual(auth.reporter, "mx.example.org")
        self.assertIn("dmarc=pass", auth.detail())

    def test_an_aligned_dkim_signature_passes_without_dmarc(self) -> None:
        auth = parse_authentication(
            _message(
                headers="Authentication-Results: mail.example.org;"
                " dkim=pass header.d=github.com; dmarc=none"
            )
        )

        self.assertIs(auth.verdict, Authenticity.PASSED)
        self.assertEqual(auth.signed_domain, "github.com")

    def test_a_subdomain_signature_is_aligned(self) -> None:
        auth = parse_authentication(
            _message(
                from_="alerts@mail.github.com",
                headers="Authentication-Results: mx; dkim=pass header.d=github.com",
            )
        )

        self.assertIs(auth.verdict, Authenticity.PASSED)

    def test_a_passing_signature_from_another_domain_is_not_a_pass(self) -> None:
        """The case the indicator exists for: signed, but not by the sender."""
        auth = parse_authentication(
            _message(
                headers="Authentication-Results: mx;"
                " dkim=pass header.d=sendgrid.net; spf=pass"
                " smtp.mailfrom=bounces.sendgrid.net"
            )
        )

        self.assertIs(auth.verdict, Authenticity.UNALIGNED)
        self.assertEqual(auth.signed_domain, "sendgrid.net")
        self.assertIn("not by github.com", auth.label())

    def test_a_failure_is_reported_as_one(self) -> None:
        auth = parse_authentication(
            _message(
                headers="Authentication-Results: mx;"
                " dkim=fail header.d=github.com; dmarc=fail header.from=github.com"
            )
        )

        self.assertIs(auth.verdict, Authenticity.FAILED)
        self.assertIn("failed", auth.label())

    def test_a_soft_failure_is_not_a_failure(self) -> None:
        """Ordinary forwarded mail softfails SPF; crying wolf trains readers."""
        auth = parse_authentication(
            _message(headers="Authentication-Results: mx; spf=softfail")
        )

        self.assertIs(auth.verdict, Authenticity.UNKNOWN)
        self.assertFalse(auth.worth_showing)

    def test_spf_alone_says_nothing_about_the_from_header(self) -> None:
        auth = parse_authentication(
            _message(
                headers="Authentication-Results: mx;"
                " spf=pass smtp.mailfrom=github.com; dkim=none; dmarc=none"
            )
        )

        self.assertIs(auth.verdict, Authenticity.UNKNOWN)

    def test_no_header_at_all_says_nothing(self) -> None:
        auth = parse_authentication(_message(headers="X-Other: x"))

        self.assertIs(auth.verdict, Authenticity.UNKNOWN)
        self.assertFalse(auth.worth_showing)
        self.assertEqual(auth.from_domain, "github.com")


class ForgedReportTest(unittest.TestCase):
    def test_only_the_receiving_server_s_own_report_is_read(self) -> None:
        """A message can arrive carrying any report the sender liked.

        The receiving server prepends its own, so the first header is
        the only one that was not supplied by the sender.
        """
        auth = parse_authentication(
            _message(
                headers=(
                    "Authentication-Results: mx.example.org;"
                    " dkim=fail header.d=github.com;"
                    " dmarc=fail header.from=github.com\n"
                    "Authentication-Results: mx.example.org;"
                    " dkim=pass header.d=github.com;"
                    " dmarc=pass header.from=github.com"
                )
            )
        )

        self.assertIs(auth.verdict, Authenticity.FAILED)
        self.assertEqual(auth.extra_reports, 1)
        self.assertIn("ignored", auth.detail())


class MalformedHeaderTest(unittest.TestCase):
    def test_comments_do_not_become_results(self) -> None:
        auth = parse_authentication(
            _message(
                headers="Authentication-Results: mx (why not);"
                " dkim=pass (good signature; dmarc=pass) header.d=github.com"
            )
        )

        self.assertIs(auth.verdict, Authenticity.PASSED)
        self.assertEqual(auth.methods, (("dkim", "pass"),))

    def test_nonsense_is_not_a_verdict(self) -> None:
        for headers in (
            "Authentication-Results: ",
            "Authentication-Results: mx",
            "Authentication-Results: mx; ;;; =; dkim=",
            "Authentication-Results: mx; unknown-method=pass",
        ):
            with self.subTest(headers=headers):
                auth = parse_authentication(_message(headers=headers))
                self.assertIs(auth.verdict, Authenticity.UNKNOWN)

    def test_a_pass_that_names_no_signer_cannot_be_called_aligned(self) -> None:
        """Safe direction: unconfirmed alignment is not a pass."""
        auth = parse_authentication(
            _message(headers="Authentication-Results: mx; dkim=pass header.d=")
        )

        self.assertIs(auth.verdict, Authenticity.UNALIGNED)
        self.assertEqual(auth.signed_domain, "")
        self.assertIn("another domain", auth.label())

    def test_a_reporter_name_is_not_taken_on_trust(self) -> None:
        """It is printed, so it may not carry anything but a host name."""
        auth = parse_authentication(
            _message(
                headers="Authentication-Results: mx\x7f[31m; dkim=pass"
                " header.d=github.com"
            )
        )

        self.assertEqual(auth.reporter, "")
        self.assertIs(auth.verdict, Authenticity.PASSED)


class RenderedTest(unittest.TestCase):
    """The verdict reaches every surface from one place on the model."""

    def test_render_message_carries_the_verdict(self) -> None:
        rendered = render_message(corpus.authenticated_message())

        self.assertIs(rendered.authentication.verdict, Authenticity.PASSED)
        self.assertEqual(rendered.authentication.signed_domain, "example.com")

    def test_an_unreported_message_says_nothing(self) -> None:
        rendered = render_message(corpus.plain_text())

        self.assertFalse(rendered.authentication.worth_showing)

    def test_the_browser_page_states_it_beside_from(self) -> None:
        page = build_browser_html(corpus.authenticated_message())

        self.assertIn("sender-check sender-passed", page)
        self.assertIn("\U0001f512", page)
        # The heading is in the summary, so the shut panel is one
        # character and the open one has a row that closes it again.
        self.assertIn(
            "<span class='sender-headline'>Sender confirmed</span></summary>", page
        )
        self.assertIn("example.com confirmed by dkim+dmarc", page)
        self.assertIn("says nothing about whether its owner is honest", page)
        # On the `From` row, because it is a statement about `From`.
        from_row = page[page.index("<td>From</td>") : page.index("<td>To</td>")]
        self.assertIn("sender-check", from_row)

    def test_the_padlock_needs_no_script_to_open(self) -> None:
        """The page is served with scripts switched off, so it uses none."""
        page = build_browser_html(corpus.authenticated_message())

        self.assertIn("<details class='sender-check", page)
        self.assertIn("<summary", page)
        self.assertNotIn("<script", page)

    def test_the_browser_page_marks_a_mismatched_signature(self) -> None:
        page = build_browser_html(
            corpus.authenticated_message(
                results="mx; dkim=pass header.d=bulk-sender.example"
            )
        )

        self.assertIn("sender-check sender-unaligned", page)
        self.assertIn("\U0001f513", page)
        self.assertIn("not by example.com", page)

    def test_an_unchecked_message_gets_an_open_padlock(self) -> None:
        """Absent is not the same as unchecked, so the lock is always there."""
        page = build_browser_html(corpus.plain_text())

        self.assertIn("sender-check sender-unknown", page)
        self.assertIn("\U0001f513", page)
        self.assertIn("Sender not checked", page)
        # Nothing to report means no summary and no method list.
        self.assertNotIn("<p class='sender-detail'", page)
        self.assertNotIn("<p>not checked</p>", page)

    def test_a_failed_check_is_not_the_same_glyph_as_an_unchecked_one(self) -> None:
        page = build_browser_html(
            corpus.authenticated_message(results="mx; dkim=fail header.d=example.com")
        )

        self.assertIn("\u26a0\ufe0f", page)
        self.assertIn("sender-check sender-failed", page)

    def test_the_senders_own_styles_come_before_ponys(self) -> None:
        """A `table{}` of the sender's must not win on being later.

        This page carries Pony's header table above the message, and the
        row stating who signed it is in that table.
        """
        page = build_browser_html(
            corpus.authenticated_message().replace(
                b"Content-Type: text/plain", b"Content-Type: text/html"
            )
        )

        self.assertLess(page.index("<style>"), page.index("table.headers{"))


class WordingTest(unittest.TestCase):
    def test_an_unknown_verdict_still_has_words(self) -> None:
        """No front end shows it, but nothing should return None either."""
        auth = parse_authentication(_message(headers="X-Other: x"))

        self.assertEqual(auth.label(), "not checked")
        self.assertEqual(auth.detail(), "")

    def test_a_failure_names_the_method_that_failed(self) -> None:
        auth = parse_authentication(
            _message(
                headers="Authentication-Results: mx;"
                " spf=pass smtp.mailfrom=example.com; dkim=fail header.d=example.com"
            )
        )

        self.assertEqual(auth.label(), "failed dkim")

    def test_a_failure_with_no_nameable_method_falls_back(self) -> None:
        """`fail` on a method `label()` does not list leaves it to the list."""
        auth = MessageAuthentication(
            verdict=Authenticity.FAILED, methods=(("dkim", "temperror"),)
        )

        self.assertEqual(auth.label(), "failed dkim")
