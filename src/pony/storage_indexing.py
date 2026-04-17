"""Project mirror-stored messages into the SQLite index."""

from __future__ import annotations

from .domain import FolderRef
from .message_projection import project_rfc822_message
from .protocols import IndexRepository, MirrorRepository


def ingest_account_from_mirror(
    *,
    mirror_repository: MirrorRepository,
    index_repository: IndexRepository,
    account_name: str,
) -> int:
    """Read mirror messages and upsert projected metadata into index."""
    upserted = 0
    folders = mirror_repository.list_folders(account_name=account_name)
    for folder in folders:
        upserted += ingest_folder_from_mirror(
            mirror_repository=mirror_repository,
            index_repository=index_repository,
            folder=folder,
        )
    return upserted


def ingest_folder_from_mirror(
    *,
    mirror_repository: MirrorRepository,
    index_repository: IndexRepository,
    folder: FolderRef,
) -> int:
    """Read one folder from mirror storage and update index rows."""
    upserted = 0
    for message_ref in mirror_repository.list_messages(folder=folder):
        raw_message = mirror_repository.get_message_bytes(message_ref=message_ref)
        projected = project_rfc822_message(
            message_ref=message_ref,
            raw_message=raw_message,
            storage_key=message_ref.message_id,
        )
        index_repository.upsert_message(message=projected)
        upserted += 1
    return upserted
