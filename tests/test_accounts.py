"""Tests for pony.accounts.

These behaviours previously lived in four separate copies (``cli``,
``mcp_server``, ``sync`` and ``main_screen``); the point of the module is
that there is now one answer to "which account is this" and "where does
its mail live", so the tests assert the contract rather than any one
caller's use of it.
"""

from __future__ import annotations

import unittest
from uuid import uuid4

from conftest import TMP_ROOT

from pony.accounts import (
    build_mirror,
    build_mirrors,
    find_account,
    find_imap_account,
    imap_accounts,
    select_imap_accounts,
)
from pony.domain import (
    AccountConfig,
    AnyAccount,
    AppConfig,
    LocalAccountConfig,
    MirrorConfig,
    SmtpConfig,
)
from pony.storage import MaildirMirrorRepository, MboxMirrorRepository


def _mirror_dir() -> MirrorConfig:
    path = TMP_ROOT / "accounts-tests" / uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return MirrorConfig(path=path, format="maildir")


def _imap(name: str = "work") -> AccountConfig:
    return AccountConfig(
        name=name,
        email_address=f"{name}@example.com",
        imap_host="imap.example.com",
        smtp=SmtpConfig(host="smtp.example.com"),
        username=name,
        credentials_source="plaintext",
        mirror=_mirror_dir(),
        password="pw",
    )


def _local(name: str = "archive", fmt: str = "maildir") -> LocalAccountConfig:
    mirror = _mirror_dir()
    return LocalAccountConfig(
        name=name,
        email_address=f"{name}@example.com",
        mirror=MirrorConfig(path=mirror.path, format=fmt),  # type: ignore[arg-type]
    )


def _config(*accounts: AnyAccount) -> AppConfig:
    return AppConfig(accounts=tuple(accounts))


class BuildMirrorTest(unittest.TestCase):
    def test_a_maildir_account_gets_a_maildir_repository(self) -> None:
        self.assertIsInstance(build_mirror(_imap()), MaildirMirrorRepository)

    def test_an_mbox_account_gets_an_mbox_repository(self) -> None:
        account = _local("mboxacct", fmt="mbox")
        self.assertIsInstance(build_mirror(account), MboxMirrorRepository)

    def test_the_repository_is_bound_to_the_account_it_was_built_from(self) -> None:
        # Asserted through the public API: a mirror answers for its own
        # account and rejects any other.
        mirror = build_mirror(_imap("work"))
        folders = mirror.list_folders(account_name="work")
        self.assertEqual([f.account_name for f in folders], ["work"])
        with self.assertRaises(ValueError):
            mirror.list_folders(account_name="someone-else")

    def test_build_mirrors_covers_every_account(self) -> None:
        config = _config(_imap("work"), _local("archive"))
        mirrors = build_mirrors(config)
        self.assertEqual(sorted(mirrors), ["archive", "work"])


class FindAccountTest(unittest.TestCase):
    def test_find_account_accepts_a_local_account(self) -> None:
        # Reading mail needs a mirror, not a server.
        config = _config(_local("archive"))
        found = find_account(config, "archive")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.name, "archive")

    def test_find_account_returns_none_for_an_unknown_name(self) -> None:
        self.assertIsNone(find_account(_config(_imap()), "nope"))

    def test_find_imap_account_rejects_a_local_account(self) -> None:
        # A local account carries neither connection details nor the
        # folder-policy fields sync and the move guards consult, so a
        # name match is not enough.
        config = _config(_local("archive"))
        self.assertIsNone(find_imap_account(config, "archive"))

    def test_find_imap_account_finds_the_imap_one_of_a_shared_name(self) -> None:
        config = _config(_local("shared"), _imap("shared"))
        found = find_imap_account(config, "shared")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertIsInstance(found, AccountConfig)


class SelectAccountsTest(unittest.TestCase):
    def test_imap_accounts_filters_out_local_ones_and_keeps_order(self) -> None:
        config = _config(_imap("a"), _local("b"), _imap("c"))
        self.assertEqual([a.name for a in imap_accounts(config)], ["a", "c"])

    def test_no_name_selects_every_imap_account(self) -> None:
        config = _config(_imap("a"), _local("b"), _imap("c"))
        self.assertEqual(
            [a.name for a in select_imap_accounts(config, None)], ["a", "c"]
        )

    def test_a_name_selects_just_that_account(self) -> None:
        config = _config(_imap("a"), _imap("c"))
        self.assertEqual([a.name for a in select_imap_accounts(config, "c")], ["c"])

    def test_an_unknown_name_selects_nothing_rather_than_raising(self) -> None:
        # Callers decide how to present "you asked for something that does
        # not exist" — the CLI exits, the TUI notifies.
        self.assertEqual(select_imap_accounts(_config(_imap("a")), "zzz"), [])

    def test_a_local_account_name_selects_nothing(self) -> None:
        config = _config(_imap("a"), _local("b"))
        self.assertEqual(select_imap_accounts(config, "b"), [])
