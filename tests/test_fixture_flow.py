"""Tests for offline fixture ingest flow."""

from __future__ import annotations

import unittest
from uuid import uuid4

from conftest import TMP_ROOT

from pony.mail.domain import AccountConfig, AppConfig, MirrorConfig, SmtpConfig
from pony.mail.fixture_flow import count_fixture_hits, run_fixture_ingest
from pony.paths import AppPaths


class FixtureFlowTestCase(unittest.TestCase):
    """Validate deterministic offline indexing fixture behavior."""

    def test_fixture_ingest_creates_searchable_message(self) -> None:
        temp_root = TMP_ROOT / "fixture-flow" / uuid4().hex
        temp_root.mkdir(parents=True, exist_ok=True)
        paths = AppPaths(
            config_file=temp_root / "config.toml",
            data_dir=temp_root / "data",
        )
        config = AppConfig(
            accounts=(
                AccountConfig(
                    name="personal",
                    email_address="user@example.com",
                    imap_host="imap.example.com",
                    smtp=SmtpConfig(host="smtp.example.com"),
                    username="user",
                    credentials_source="plaintext",
                    mirror=MirrorConfig(
                        path=temp_root / "mirrors" / "personal",
                        format="maildir",
                    ),
                ),
            ),
        )

        created = run_fixture_ingest(config=config, paths=paths)
        hits = count_fixture_hits(paths=paths, account_name="personal")

        self.assertEqual(created, 1)
        self.assertEqual(hits, 1)
