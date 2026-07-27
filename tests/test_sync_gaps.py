"""Failure-containment and fallback coverage for the sync engine."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

import pony.sync as sync_module
from pony.domain import (
    AccountConfig,
    AppConfig,
    FolderConfig,
    FolderQuickStatus,
    FolderRef,
    IndexedMessage,
    MessageFlag,
    MessageRef,
    MessageStatus,
    MirrorConfig,
    SmtpConfig,
)
from pony.sync import (
    AccountSyncPlan,
    FolderSyncPlan,
    FolderSyncResult,
    ImapSyncService,
    PushAppendOp,
    PushMoveOp,
    ReUploadOp,
    SyncPlan,
)


def _account(name: str = "personal") -> AccountConfig:
    return AccountConfig(
        name=name,
        email_address=f"{name}@example.com",
        imap_host="imap.example.com",
        smtp=SmtpConfig(host="smtp.example.com"),
        username=name,
        credentials_source="plaintext",
        mirror=MirrorConfig(path=Path("/tmp/pony-sync-test"), format="maildir"),
        folders=FolderConfig(),
    )


def _service(
    *,
    accounts: tuple[AccountConfig, ...] | None = None,
    index: mock.MagicMock | None = None,
    mirror: mock.MagicMock | None = None,
    session: mock.MagicMock | None = None,
) -> tuple[ImapSyncService, mock.MagicMock, mock.MagicMock, mock.MagicMock]:
    account_set = accounts or (_account(),)
    index = index or mock.MagicMock()
    mirror = mirror or mock.MagicMock()
    session = session or mock.MagicMock()
    credentials = mock.MagicMock()
    credentials.get_password.return_value = "secret"
    service = ImapSyncService(
        config=AppConfig(accounts=account_set),
        mirror_factory=lambda _account: mirror,
        index=index,
        credentials=credentials,
        session_factory=lambda _account, _password: session,
    )
    return service, index, mirror, session


def _row(
    *,
    status: MessageStatus = MessageStatus.ACTIVE,
    uid: int | None = 7,
    storage_key: str = "message-key",
) -> IndexedMessage:
    return IndexedMessage(
        message_ref=MessageRef("personal", "Archive", 11),
        sender="Alex Example <alex@example.com>",
        recipients="reader@example.com",
        cc="",
        subject="Status report",
        body_preview="Body",
        storage_key=storage_key,
        local_flags=frozenset({MessageFlag.SEEN}),
        base_flags=frozenset(),
        local_status=status,
        received_at=datetime(2026, 1, 2, tzinfo=UTC),
        uid=uid,
        source_folder="INBOX" if status == MessageStatus.PENDING_MOVE else None,
        source_uid=7 if status == MessageStatus.PENDING_MOVE else None,
    )


def _raw_message() -> bytes:
    message = EmailMessage()
    message["From"] = "alex@example.com"
    message["To"] = "reader@example.com"
    message["Subject"] = "Status report"
    message["Message-ID"] = "<status-report@example.com>"
    message.set_content("Body")
    return message.as_bytes()


def test_plan_raises_only_when_every_selected_account_fails() -> None:
    service, _, _, _ = _service(accounts=(_account("first"), _account("second")))
    service._plan_account = mock.MagicMock(side_effect=OSError("offline"))

    with (
        mock.patch.object(sync_module.logger, "exception"),
        mock.patch.object(sync_module, "logger"),
    ):
        try:
            service.plan()
        except RuntimeError as error:
            assert "first: offline" in str(error)
            assert "second: offline" in str(error)
        else:
            raise AssertionError("planning failures should have been reported")


def test_plan_keeps_successful_accounts_when_another_account_fails() -> None:
    service, _, _, _ = _service(accounts=(_account("first"), _account("second")))
    successful = AccountSyncPlan(
        account_name="first",
        folders=(),
        creates=("Archive",),
    )
    service._plan_account = mock.MagicMock(side_effect=[successful, OSError("offline")])

    plan = service.plan()

    assert plan.accounts == (successful,)


def test_execute_skips_removed_account_and_contains_account_failure() -> None:
    service, index, _, _ = _service()
    index.list_indexed_accounts.return_value = []
    index.purge_expired_trash.return_value = []
    plan = SyncPlan(
        accounts=(
            AccountSyncPlan(account_name="removed", folders=()),
            AccountSyncPlan(account_name="personal", folders=()),
        )
    )
    service._execute_account_plan = mock.MagicMock(side_effect=OSError("offline"))

    result = service.execute(plan)

    assert result.accounts == ()
    service._execute_account_plan.assert_called_once()


def test_safe_status_without_reconnect_returns_none() -> None:
    service, _, _, session = _service()
    session.folder_quick_status.side_effect = EOFError("closed")

    assert service._safe_status(session, "INBOX", reconnect=None, scan_ms={}) is None


def test_safe_status_reconnects_once_and_returns_status() -> None:
    service, _, _, session = _service()
    replacement = mock.MagicMock()
    status = FolderQuickStatus(1, 3, 2, 4)
    session.folder_quick_status.side_effect = OSError("closed")
    replacement.folder_quick_status.return_value = status

    result = service._safe_status(
        session,
        "INBOX",
        reconnect=lambda: replacement,
        scan_ms={},
    )

    assert result == status


def test_safe_status_returns_none_when_retry_also_fails() -> None:
    service, _, _, session = _service()
    session.folder_quick_status.side_effect = OSError("closed")
    replacement = mock.MagicMock()
    replacement.folder_quick_status.side_effect = EOFError("still closed")

    assert (
        service._safe_status(
            session,
            "INBOX",
            reconnect=lambda: replacement,
            scan_ms={},
        )
        is None
    )


def test_plan_account_reconnects_and_logs_out_replacement_session() -> None:
    first = mock.MagicMock()
    replacement = mock.MagicMock()
    first.logout.side_effect = OSError("already closed")
    service, _, _, _ = _service()
    service._session_factory = mock.MagicMock(side_effect=[first, replacement])
    expected = AccountSyncPlan(account_name="personal", folders=())

    def plan_folders(**kwargs: object) -> AccountSyncPlan:
        reconnect = kwargs["reconnect"]
        assert callable(reconnect)
        assert reconnect() is replacement
        return expected

    service._plan_folders = mock.MagicMock(side_effect=plan_folders)

    assert service._plan_account(account=_account()) == expected
    replacement.logout.assert_called_once_with()


def test_execute_account_plan_contains_create_and_folder_retry_failures() -> None:
    initial = mock.MagicMock()
    replacement = mock.MagicMock()
    final_session = mock.MagicMock()
    service, _, mirror, _ = _service()
    service._session_factory = mock.MagicMock(
        side_effect=[initial, replacement, final_session]
    )
    initial.create_folder.side_effect = [None, OSError("create failed")]
    changed = FolderSyncResult(folder_name="Retried", fetched=1)
    service._execute_folder_plan = mock.MagicMock(
        side_effect=[
            OSError("connection lost"),
            changed,
            EOFError("connection lost again"),
            EOFError("retry failed"),
        ]
    )
    progress = mock.MagicMock()
    plan = AccountSyncPlan(
        account_name="personal",
        creates=("Created", "Failed"),
        folders=(
            FolderSyncPlan("Needs confirmation", 1, 0, (), needs_confirmation=True),
            FolderSyncPlan("Retried", 1, 0, ()),
            FolderSyncPlan("Skipped after retry", 1, 0, ()),
        ),
        skipped_folders=("Previously skipped",),
    )

    result = service._execute_account_plan(
        account=_account(),
        plan=plan,
        confirmed_folders=frozenset(),
        progress=progress,
    )

    assert result.folders == (changed,)
    assert result.skipped_folders == (
        "Previously skipped",
        "Needs confirmation",
        "Skipped after retry",
    )
    assert initial.create_folder.call_count == 2
    assert service._execute_folder_plan.call_count == 4
    initial.logout.assert_called_once_with()
    replacement.logout.assert_called_once_with()
    final_session.logout.assert_called_once_with()
    assert any(
        "created Created" in call.args[0].message for call in progress.call_args_list
    )
    assert any("1 fetched" in call.args[0].message for call in progress.call_args_list)


def test_reupload_handles_missing_row_and_mirror_failure() -> None:
    service, index, mirror, session = _service()
    op = ReUploadOp(MessageRef("personal", "Archive", 11), frozenset())
    now = datetime.now(tz=UTC)
    index.get_message.return_value = None
    assert not service._execute_reupload(
        op,
        account=_account(),
        folder_name="Archive",
        session=session,
        now=now,
    )

    index.get_message.return_value = _row()
    mirror.get_message_bytes.side_effect = OSError("missing")
    assert not service._execute_reupload(
        op,
        account=_account(),
        folder_name="Archive",
        session=session,
        now=now,
    )
    session.append_message.assert_not_called()


def test_reupload_without_appenduid_clears_server_sync_fields() -> None:
    service, index, mirror, session = _service()
    row = _row()
    index.get_message.return_value = row
    mirror.get_message_bytes.return_value = _raw_message()
    session.append_message.return_value = None
    op = ReUploadOp(row.message_ref, row.local_flags, frozenset({"$Label"}))

    assert service._execute_reupload(
        op,
        account=_account(),
        folder_name="Archive",
        session=session,
        now=datetime.now(tz=UTC),
    )
    updated = index.update_message.call_args.kwargs["message"]
    assert updated.uid is None
    assert updated.server_flags == frozenset()
    assert updated.synced_at is None


def test_push_append_handles_missing_row_and_mirror_failure() -> None:
    service, index, mirror, session = _service()
    ref = MessageRef("personal", "Archive", 11)
    op = PushAppendOp(ref)
    index.get_message.return_value = None
    assert not service._execute_push_append(
        op,
        account=_account(),
        folder_name="Archive",
        session=session,
        mirror=mirror,
        now=datetime.now(tz=UTC),
    )

    index.get_message.return_value = _row(uid=None)
    mirror.get_message_bytes.side_effect = OSError("missing")
    assert not service._execute_push_append(
        op,
        account=_account(),
        folder_name="Archive",
        session=session,
        mirror=mirror,
        now=datetime.now(tz=UTC),
    )


def test_push_append_without_appenduid_promotes_pending_move() -> None:
    service, index, mirror, session = _service()
    row = _row(status=MessageStatus.PENDING_MOVE, uid=None)
    index.get_message.return_value = row
    mirror.get_message_bytes.return_value = _raw_message()
    session.append_message.return_value = None

    assert service._execute_push_append(
        PushAppendOp(row.message_ref),
        account=_account(),
        folder_name="Archive",
        session=session,
        mirror=mirror,
        now=datetime.now(tz=UTC),
    )
    updated = index.update_message.call_args.kwargs["message"]
    assert updated.local_status == MessageStatus.ACTIVE
    assert updated.source_folder is None
    assert updated.source_uid is None


def test_push_move_handles_missing_row_failure_and_missing_copyuid() -> None:
    service, index, mirror, session = _service()
    row = _row(status=MessageStatus.PENDING_MOVE, uid=None)
    op = PushMoveOp(row.message_ref, "INBOX", 7, "Archive")
    now = datetime.now(tz=UTC)
    index.get_message.return_value = None
    assert not service._execute_push_move(
        op, account=_account(), session=session, mirror=mirror, now=now
    )

    index.get_message.return_value = row
    session.move_message.side_effect = OSError("move failed")
    assert not service._execute_push_move(
        op, account=_account(), session=session, mirror=mirror, now=now
    )

    session.move_message.side_effect = None
    session.move_message.return_value = None
    assert service._execute_push_move(
        op, account=_account(), session=session, mirror=mirror, now=now
    )
    updated = index.update_message.call_args.kwargs["message"]
    assert updated.local_status == MessageStatus.ACTIVE
    assert updated.uid is None
    assert updated.synced_at is None


def test_uidvalidity_reset_skips_empty_keys_and_suppresses_delete_errors() -> None:
    service, index, mirror, _ = _service()
    folder = FolderRef("personal", "Archive")
    index.list_folder_messages.return_value = [
        _row(storage_key=""),
        dataclasses.replace(_row(), message_ref=MessageRef("personal", "Archive", 12)),
        dataclasses.replace(
            _row(),
            message_ref=MessageRef("personal", "Archive", 13),
            local_status=MessageStatus.TRASHED,
        ),
    ]
    mirror.delete_message.side_effect = OSError("already gone")

    service._execute_uidvalidity_reset(
        account=_account(), folder_ref=folder, mirror=mirror
    )

    index.clear_uids_for_folder.assert_called_once_with(
        account_name="personal", folder_name="Archive"
    )
    mirror.delete_message.assert_called_once_with(
        folder=folder, storage_key="message-key"
    )


def test_ingest_raw_rejects_empty_and_storage_failure() -> None:
    service, index, mirror, _ = _service()
    folder = FolderRef("personal", "INBOX")
    assert not service._ingest_raw(
        account=_account(),
        folder_ref=folder,
        mirror=mirror,
        uid=1,
        message_id="<empty@example.com>",
        server_flags=frozenset(),
        extra_imap_flags=frozenset(),
        raw=b"",
    )

    mirror.store_message_async.side_effect = OSError("disk full")
    assert not service._ingest_raw(
        account=_account(),
        folder_ref=folder,
        mirror=mirror,
        uid=2,
        message_id="<full@example.com>",
        server_flags=frozenset(),
        extra_imap_flags=frozenset(),
        raw=_raw_message(),
    )
    index.insert_message.assert_not_called()


def test_ingest_raw_uses_synchronous_store_when_async_is_unavailable() -> None:
    service, index, _, _ = _service()
    folder = FolderRef("personal", "INBOX")

    class SyncMirror:
        def store_message(self, **_kwargs: object) -> str:
            return "stored-key"

    assert service._ingest_raw(
        account=_account(),
        folder_ref=folder,
        mirror=SyncMirror(),  # type: ignore[arg-type]
        uid=3,
        message_id="<stored@example.com>",
        server_flags=frozenset({MessageFlag.SEEN}),
        extra_imap_flags=frozenset({"$Label"}),
        raw=_raw_message(),
    )
    inserted = index.insert_message.call_args.kwargs["message"]
    assert inserted.storage_key == "stored-key"
    assert inserted.uid == 3
    assert inserted.extra_imap_flags == frozenset({"$Label"})
