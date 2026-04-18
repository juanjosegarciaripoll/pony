"""Project mirror-stored messages into the SQLite index."""

from __future__ import annotations

import re

from .domain import FolderRef, MessageRef
from .message_projection import project_rfc822_message
from .protocols import IndexRepository, MirrorRepository

# Pull the RFC 5322 Message-ID header out of the raw bytes so the index's
# MessageRef carries the same identity the IMAP sync path produces.  Falls
# back to the storage_key when the header is missing (some legacy or
# malformed messages).
_MESSAGE_ID_RE = re.compile(
    rb"^Message-ID:\s*(.*(?:\r?\n[ \t]+.*)*)",
    re.MULTILINE | re.IGNORECASE,
)


def _extract_message_id(raw: bytes) -> str:
    match = _MESSAGE_ID_RE.search(raw)
    if match is None:
        return ""
    value = match.group(1).decode("ascii", errors="replace")
    return " ".join(value.split())


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
    for storage_key in mirror_repository.list_messages(folder=folder):
        raw_message = mirror_repository.get_message_bytes(
            folder=folder, storage_key=storage_key,
        )
        rfc5322_id = _extract_message_id(raw_message) or storage_key
        message_ref = MessageRef(
            account_name=folder.account_name,
            folder_name=folder.folder_name,
            rfc5322_id=rfc5322_id,
        )
        projected = project_rfc822_message(
            message_ref=message_ref,
            raw_message=raw_message,
            storage_key=storage_key,
        )
        index_repository.upsert_message(message=projected)
        upserted += 1
    return upserted
