"""Focused coverage for CLI helper workflows and recovery paths."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import pony.cli as cli
from pony.config import ConfigError
from pony.domain import AppConfig
from pony.paths import AppPaths
from pony.storage_indexing import RescanResult


def _paths(root: Path) -> AppPaths:
    return AppPaths(
        config_file=root / "config" / "config.toml",
        data_dir=root / "data",
        state_dir=root / "state",
        cache_dir=root / "cache",
        log_dir=root / "state" / "logs",
        index_db_file=root / "data" / "index.sqlite3",
    )


@pytest.mark.parametrize("content", ["not json", "[]"])
def test_load_scan_state_rejects_corrupt_or_non_mapping_data(
    tmp_path: Path,
    content: str,
) -> None:
    state_file = tmp_path / "scan-state.json"
    state_file.write_text(content, encoding="utf-8")

    assert cli._load_scan_state(state_file) == {}


def test_scan_state_roundtrip_filters_invalid_entries(tmp_path: Path) -> None:
    state_file = tmp_path / "nested" / "scan-state.json"
    cli._save_scan_state(state_file, {"local": {"INBOX": 41}})

    assert cli._load_scan_state(state_file) == {"local": {"INBOX": 41}}
    state_file.write_text(
        '{"local": {"INBOX": 42, "Drafts": "bad"}, "bad": 7}',
        encoding="utf-8",
    )
    assert cli._load_scan_state(state_file) == {"local": {"INBOX": 42}}


def test_load_scan_state_missing_file_is_empty(tmp_path: Path) -> None:
    assert cli._load_scan_state(tmp_path / "missing.json") == {}


def test_rescan_progress_reports_changed_messages(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def rescan(**kwargs: object) -> RescanResult:
        kwargs["on_folder_scan"]("INBOX")  # type: ignore[operator]
        kwargs["on_plan"](RescanResult(added=2, removed=1))  # type: ignore[operator]
        kwargs["progress"]("INBOX", 1, 2)  # type: ignore[operator]
        kwargs["progress"]("INBOX", 2, 2)  # type: ignore[operator]
        return RescanResult(added=2, removed=1)

    with mock.patch.object(cli, "rescan_local_account", side_effect=rescan):
        cli._rescan_local_with_cli_progress(
            mirror=mock.MagicMock(),
            index=mock.MagicMock(),
            account_name="local",
            scan_state={},
        )

    error = capsys.readouterr().err
    assert "Scanning local mirror" in error
    assert "2 new, 1 removed" in error
    assert "INBOX: 2/2" in error


@pytest.mark.parametrize(
    ("walked", "expected"),
    [
        (True, "Local mirror up to date"),
        (False, "Local mirror unchanged (all folders cached)"),
    ],
)
def test_rescan_progress_reports_unchanged_state(
    capsys: pytest.CaptureFixture[str],
    walked: bool,
    expected: str,
) -> None:
    def rescan(**kwargs: object) -> RescanResult:
        if walked:
            kwargs["on_folder_scan"]("Archive")  # type: ignore[operator]
        return RescanResult(added=0, removed=0)

    with mock.patch.object(cli, "rescan_local_account", side_effect=rescan):
        cli._rescan_local_with_cli_progress(
            mirror=mock.MagicMock(),
            index=mock.MagicMock(),
            account_name="local",
            scan_state={},
        )

    assert expected in capsys.readouterr().err


def test_run_pager_uses_less_when_it_succeeds() -> None:
    completed = SimpleNamespace(returncode=0)
    with (
        mock.patch.object(cli.shutil, "which", return_value="/usr/bin/less"),
        mock.patch.object(cli.subprocess, "run", return_value=completed) as run,
        mock.patch.object(cli, "print") as fallback_print,
    ):
        cli._run_pager("one\ntwo")

    run.assert_called_once_with(
        ["/usr/bin/less", "-FRSX"],
        input=b"one\ntwo",
        check=False,
    )
    fallback_print.assert_not_called()


def test_run_pager_falls_back_and_stops_when_user_quits() -> None:
    with (
        mock.patch.object(cli.shutil, "which", return_value=None),
        mock.patch.object(
            cli.shutil,
            "get_terminal_size",
            return_value=os.terminal_size((80, 3)),
        ),
        mock.patch.object(cli, "input", return_value="q"),
        mock.patch.object(cli, "print") as output,
    ):
        cli._run_pager("one\ntwo\nthree")

    output.assert_called_once_with("one")


def test_run_pager_recovers_from_less_terminal_and_input_errors() -> None:
    with (
        mock.patch.object(cli.shutil, "which", return_value="less"),
        mock.patch.object(cli.subprocess, "run", side_effect=OSError("cannot run")),
        mock.patch.object(
            cli.shutil,
            "get_terminal_size",
            side_effect=OSError("no terminal"),
        ),
        mock.patch.object(cli, "input", side_effect=EOFError),
        mock.patch.object(cli, "print") as output,
    ):
        cli._run_pager("\n".join(str(line) for line in range(30)))

    assert output.call_count == 1


def test_require_config_noninteractive_reports_original_error(tmp_path: Path) -> None:
    error = ConfigError("broken value")
    with (
        mock.patch.object(cli, "load_config", side_effect=error),
        mock.patch.object(cli.sys.stdin, "isatty", return_value=False),
        pytest.raises(SystemExit, match="Configuration error: broken value"),
    ):
        cli.require_config(tmp_path / "config.toml")


def test_require_config_can_run_setup_then_reload(tmp_path: Path) -> None:
    config = AppConfig(accounts=())
    paths = _paths(tmp_path)
    with (
        mock.patch.object(
            cli,
            "load_config",
            side_effect=[ConfigError("missing"), config],
        ),
        mock.patch.object(cli.sys.stdin, "isatty", return_value=True),
        mock.patch.object(cli.AppPaths, "default", return_value=paths),
        mock.patch.object(cli, "input", return_value=""),
        mock.patch.object(cli, "run_account_add_interactive", return_value=0),
    ):
        assert cli.require_config(paths.config_file) is config


def test_require_config_declined_setup_reports_error(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    with (
        mock.patch.object(cli, "load_config", side_effect=ConfigError("missing")),
        mock.patch.object(cli.sys.stdin, "isatty", return_value=True),
        mock.patch.object(cli.AppPaths, "default", return_value=paths),
        mock.patch.object(cli, "input", return_value="no"),
        mock.patch.object(cli, "run_account_add_interactive") as setup,
        pytest.raises(SystemExit, match="Configuration error: missing"),
    ):
        cli.require_config(paths.config_file)

    setup.assert_not_called()


def test_require_config_can_edit_then_reload(tmp_path: Path) -> None:
    config = AppConfig(accounts=())
    paths = _paths(tmp_path)
    paths.config_file.parent.mkdir(parents=True)
    paths.config_file.touch()
    with (
        mock.patch.object(
            cli,
            "load_config",
            side_effect=[ConfigError("broken"), config],
        ),
        mock.patch.object(cli.sys.stdin, "isatty", return_value=True),
        mock.patch.object(cli.AppPaths, "default", return_value=paths),
        mock.patch.object(cli, "input", return_value="yes"),
        mock.patch.object(cli, "run_config_edit") as edit,
    ):
        assert cli.require_config(paths.config_file) is config

    edit.assert_called_once_with(paths=paths, config_path=paths.config_file)


def test_require_config_reports_failed_retry_after_edit(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.config_file.parent.mkdir(parents=True)
    paths.config_file.touch()
    with (
        mock.patch.object(
            cli,
            "load_config",
            side_effect=[ConfigError("broken"), ConfigError("still broken")],
        ),
        mock.patch.object(cli.sys.stdin, "isatty", return_value=True),
        mock.patch.object(cli.AppPaths, "default", return_value=paths),
        mock.patch.object(cli, "input", return_value="yes"),
        mock.patch.object(cli, "run_config_edit") as edit,
        pytest.raises(SystemExit, match="Configuration still invalid: still broken"),
    ):
        cli.require_config(paths.config_file)

    edit.assert_called_once_with(paths=paths, config_path=paths.config_file)
