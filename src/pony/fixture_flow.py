"""Offline fixture ingestion flow used in early development phases."""

from __future__ import annotations

from email.message import EmailMessage

from .domain import AppConfig, MessageRef, SearchQuery
from .index_store import SqliteIndexRepository
from .message_projection import project_rfc822_message
from .paths import AppPaths


def run_fixture_ingest(*, config: AppConfig, paths: AppPaths) -> int:
    """Index one deterministic parsed fixture message per configured account."""
    repository = SqliteIndexRepository(database_path=paths.index_db_file)
    repository.initialize()

    created_count = 0
    for account in config.accounts:
        message_ref = MessageRef(
            account_name=account.name,
            folder_name="INBOX",
            rfc5322_id=f"fixture-{account.name}",
        )
        raw_fixture = _fixture_message_bytes(
            account_name=account.name, to_address=account.email_address
        )
        fixture_message = project_rfc822_message(
            message_ref=message_ref,
            raw_message=raw_fixture,
            storage_key=message_ref.rfc5322_id,
        )
        repository.upsert_message(message=fixture_message)
        created_count += 1

    return created_count


def count_fixture_hits(*, paths: AppPaths, account_name: str) -> int:
    """Count fixture entries by searching subject/body text."""
    repository = SqliteIndexRepository(database_path=paths.index_db_file)
    repository.initialize()
    hits = repository.search(
        query=SearchQuery(text="fixture message"),
        account_name=account_name,
    )
    return len(hits)


def _fixture_message_bytes(*, account_name: str, to_address: str) -> bytes:
    message = EmailMessage()
    message["From"] = "fixture.sender@example.com"
    message["To"] = to_address
    message["Subject"] = f"Fixture message for {account_name}"
    message["Date"] = "Fri, 10 Apr 2026 10:00:00 +0000"
    message.set_content(
        "This is a local fixture message for offline indexing tests.\n"
        "The body includes stable text for search assertions.\n",
    )
    return message.as_bytes()
