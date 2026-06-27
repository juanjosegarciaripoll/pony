"""Configuration parsing tests."""

from __future__ import annotations

import unittest
from pathlib import Path
from uuid import uuid4

from conftest import TMP_ROOT

from pony.config import ConfigError, load_config, parse_config
from pony.domain import FolderConfig, LocalAccountConfig


class ConfigParsingTestCase(unittest.TestCase):
    """Validate the dependency-free config loader."""

    def test_parse_config_resolves_relative_mirror_paths(self) -> None:
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        config = parse_config(sample_config(), base_dir=base_dir)
        account = config.accounts[0]
        self.assertEqual(account.name, "personal")
        self.assertEqual(account.mirror.format, "maildir")
        self.assertTrue(account.mirror.path.is_absolute())

    def test_parse_config_rejects_invalid_mirror_format(self) -> None:
        # Construct the invalid data directly rather than mutating nested
        # dicts returned by sample_config(): isinstance-narrowed dict values
        # have type dict[Unknown, Unknown] in strict mode, which prevents
        # further typed access.
        data: dict[str, object] = {
            "config_version": 2,
            "accounts": [
                {
                    "name": "personal",
                    "email_address": "user@example.com",
                    "imap_host": "imap.example.com",
                    "smtp": {"host": "smtp.example.com"},
                    "username": "user",
                    "credentials_source": "plaintext",
                    "mirror": {
                        "path": "mirrors/personal",
                        "format": "not-a-format",
                        "trash_retention_days": 30,
                    },
                },
            ],
        }
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(ConfigError):
            parse_config(data, base_dir=base_dir)

    def test_folders_defaults_to_empty_policy(self) -> None:
        from pony.domain import AccountConfig

        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        config = parse_config(sample_config(), base_dir=base_dir)
        acc = config.accounts[0]
        assert isinstance(acc, AccountConfig)
        self.assertEqual(acc.folders, FolderConfig())

    def test_folders_parsed_from_dict(self) -> None:
        from pony.domain import AccountConfig

        data = sample_config()
        account = data["accounts"][0]  # type: ignore[index]
        account["folders"] = {
            "include": ["INBOX", "Archive"],
            "exclude": ["Spam"],
            "read_only": ["Sent"],
        }
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        config = parse_config(data, base_dir=base_dir)
        acc = config.accounts[0]
        assert isinstance(acc, AccountConfig)
        folders = acc.folders
        self.assertEqual(folders.include, ("INBOX", "Archive"))
        self.assertEqual(folders.exclude, ("Spam",))
        self.assertEqual(folders.read_only, ("Sent",))

    def test_folders_invalid_entry_raises(self) -> None:
        data = sample_config()
        account = data["accounts"][0]  # type: ignore[index]
        account["folders"] = {"include": ["INBOX", 42]}
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(ConfigError):
            parse_config(data, base_dir=base_dir)

    def test_folders_invalid_regex_raises(self) -> None:
        data = sample_config()
        account = data["accounts"][0]  # type: ignore[index]
        account["folders"] = {"exclude": ["["]}
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(ConfigError):
            parse_config(data, base_dir=base_dir)

    def test_parse_local_account(self) -> None:
        data: dict[str, object] = {
            "config_version": 2,
            "accounts": [
                {
                    "account_type": "local",
                    "name": "archive",
                    "email_address": "me@example.com",
                    "mirror": {
                        "path": "mirrors/archive",
                        "format": "maildir",
                        "trash_retention_days": 7,
                    },
                },
            ],
        }
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        config = parse_config(data, base_dir=base_dir)
        self.assertEqual(len(config.accounts), 1)
        acc = config.accounts[0]
        self.assertIsInstance(acc, LocalAccountConfig)
        self.assertEqual(acc.name, "archive")
        self.assertEqual(acc.email_address, "me@example.com")
        self.assertEqual(acc.mirror.format, "maildir")

    def test_parse_local_account_no_imap_fields_required(self) -> None:
        """A local account must not require imap_host, smtp block, or credentials."""
        data: dict[str, object] = {
            "config_version": 2,
            "accounts": [
                {
                    "account_type": "local",
                    "name": "local-only",
                    "email_address": "user@example.com",
                    "mirror": {
                        "path": "mirrors/local",
                        "format": "mbox",
                        "trash_retention_days": 30,
                    },
                },
            ],
        }
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        config = parse_config(data, base_dir=base_dir)
        self.assertIsInstance(config.accounts[0], LocalAccountConfig)

    def test_missing_config_version_is_rejected(self) -> None:
        """No silent migration: the parser requires the version declaration."""
        data = sample_config()
        del data["config_version"]
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(ConfigError) as ctx:
            parse_config(data, base_dir=base_dir)
        self.assertIn("config_version", str(ctx.exception))

    def test_wrong_config_version_is_rejected(self) -> None:
        data = sample_config()
        data["config_version"] = 1
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(ConfigError) as ctx:
            parse_config(data, base_dir=base_dir)
        self.assertIn("config_version", str(ctx.exception))

    def test_local_account_with_smtp_can_send(self) -> None:
        """A local account that carries an [smtp] block plus credentials
        reports ``can_send`` as True and ends up in the composer."""
        from pony.domain import LocalAccountConfig

        data: dict[str, object] = {
            "config_version": 2,
            "accounts": [
                {
                    "account_type": "local",
                    "name": "outbound-only",
                    "email_address": "me@example.com",
                    "username": "me@example.com",
                    "credentials_source": "plaintext",
                    "password": "x",
                    "smtp": {"host": "smtp.example.com"},
                    "mirror": {"path": "mirrors/out", "format": "maildir"},
                },
            ],
        }
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        config = parse_config(data, base_dir=base_dir)
        acc = config.accounts[0]
        self.assertIsInstance(acc, LocalAccountConfig)
        self.assertTrue(acc.can_send)

    def test_local_account_without_smtp_cannot_send(self) -> None:
        from pony.domain import LocalAccountConfig

        data: dict[str, object] = {
            "config_version": 2,
            "accounts": [
                {
                    "account_type": "local",
                    "name": "archive-only",
                    "email_address": "me@example.com",
                    "mirror": {"path": "m", "format": "maildir"},
                },
            ],
        }
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        config = parse_config(data, base_dir=base_dir)
        acc = config.accounts[0]
        self.assertIsInstance(acc, LocalAccountConfig)
        self.assertFalse(acc.can_send)

    def test_local_account_with_smtp_but_no_credentials_is_rejected(self) -> None:
        """If ``smtp`` is set on a local account, credentials become
        mandatory — otherwise sending would fail at runtime."""
        data: dict[str, object] = {
            "config_version": 2,
            "accounts": [
                {
                    "account_type": "local",
                    "name": "bad",
                    "email_address": "me@example.com",
                    "smtp": {"host": "smtp.example.com"},
                    "mirror": {"path": "m", "format": "maildir"},
                },
            ],
        }
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(ConfigError):
            parse_config(data, base_dir=base_dir)

    def test_imap_account_can_send(self) -> None:
        from pony.domain import AccountConfig

        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        config = parse_config(sample_config(), base_dir=base_dir)
        acc = config.accounts[0]
        assert isinstance(acc, AccountConfig)
        self.assertTrue(acc.can_send)

    def test_parse_unknown_account_type_raises(self) -> None:
        data: dict[str, object] = {
            "config_version": 2,
            "accounts": [
                {
                    "account_type": "ftp",
                    "name": "bad",
                    "email_address": "x@example.com",
                    "mirror": {"path": "m", "format": "maildir"},
                },
            ],
        }
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(ConfigError):
            parse_config(data, base_dir=base_dir)

    def test_imap_account_default_when_type_absent(self) -> None:
        """Existing configs without account_type should still parse as IMAP accounts."""
        from pony.domain import AccountConfig

        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        config = parse_config(sample_config(), base_dir=base_dir)
        self.assertIsInstance(config.accounts[0], AccountConfig)

    def test_load_config_from_toml_file(self) -> None:
        temp_root = TMP_ROOT / "config-load" / uuid4().hex
        temp_root.mkdir(parents=True, exist_ok=True)
        config_path = temp_root / "config.toml"
        config_path.write_text(sample_toml_config(), encoding="utf-8")

        config = load_config(config_path)
        self.assertEqual(len(config.accounts), 1)
        self.assertEqual(config.accounts[0].mirror.format, "maildir")

    def test_bbdb_path_parsed(self) -> None:
        from pathlib import Path

        data = sample_config()
        data["bbdb_path"] = "/tmp/my.bbdb"
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        config = parse_config(data, base_dir=base_dir)
        self.assertEqual(config.bbdb_path, Path("/tmp/my.bbdb"))

    def test_bbdb_path_defaults_to_none(self) -> None:
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        config = parse_config(sample_config(), base_dir=base_dir)
        self.assertIsNone(config.bbdb_path)

    def test_downloads_path_parsed(self) -> None:
        from pathlib import Path

        data = sample_config()
        data["downloads_path"] = "/tmp/mail-attachments"
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        config = parse_config(data, base_dir=base_dir)
        self.assertEqual(config.downloads_path, Path("/tmp/mail-attachments"))

    def test_downloads_path_defaults_to_none(self) -> None:
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        config = parse_config(sample_config(), base_dir=base_dir)
        self.assertIsNone(config.downloads_path)

    def test_theme_parsed(self) -> None:
        data = sample_config()
        data["theme"] = "nord"
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        config = parse_config(data, base_dir=base_dir)
        self.assertEqual(config.theme, "nord")

    def test_theme_defaults_to_none(self) -> None:
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        config = parse_config(sample_config(), base_dir=base_dir)
        self.assertIsNone(config.theme)

    def test_background_sync_defaults(self) -> None:
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        config = parse_config(sample_config(), base_dir=base_dir)
        self.assertFalse(config.background_sync_enabled)
        self.assertEqual(config.background_sync_interval_seconds, 600)

    def test_background_sync_parsed(self) -> None:
        data = sample_config()
        data["background_sync_enabled"] = True
        data["background_sync_interval_seconds"] = 60
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        config = parse_config(data, base_dir=base_dir)
        self.assertTrue(config.background_sync_enabled)
        self.assertEqual(config.background_sync_interval_seconds, 60)

    def test_background_sync_interval_non_positive_raises(self) -> None:
        data = sample_config()
        data["background_sync_interval_seconds"] = 0
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(ConfigError):
            parse_config(data, base_dir=base_dir)

    def test_archive_folder_parsed(self) -> None:
        from pony.domain import AccountConfig

        data = sample_config()
        account_raw = data["accounts"][0]  # type: ignore[index]
        account_raw["archive_folder"] = "Archive"
        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        config = parse_config(data, base_dir=base_dir)
        account = config.accounts[0]
        assert isinstance(account, AccountConfig)
        self.assertEqual(account.archive_folder, "Archive")

    def test_archive_folder_defaults_to_none(self) -> None:
        from pony.domain import AccountConfig

        base_dir = TMP_ROOT / "config-base"
        base_dir.mkdir(parents=True, exist_ok=True)
        config = parse_config(sample_config(), base_dir=base_dir)
        account = config.accounts[0]
        assert isinstance(account, AccountConfig)
        self.assertIsNone(account.archive_folder)


class ConfigValidationErrorsTestCase(unittest.TestCase):
    """Tests for validation error branches in config parsing."""

    def _base_dir(self) -> Path:
        d = TMP_ROOT / "cfg-val" / uuid4().hex
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_load_config_invalid_toml_raises(self) -> None:

        temp = TMP_ROOT / "bad-toml" / uuid4().hex
        temp.mkdir(parents=True, exist_ok=True)
        cfg = temp / "config.toml"
        cfg.write_text("[unclosed", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(cfg)

    def test_load_config_json_file_reads(self) -> None:
        """A .json config file is parsed via json.loads (line 84)."""
        import json

        data = sample_config()
        temp = TMP_ROOT / "json-cfg" / uuid4().hex
        temp.mkdir(parents=True, exist_ok=True)
        cfg = temp / "config.json"
        cfg.write_text(json.dumps(data), encoding="utf-8")
        config = load_config(cfg)
        self.assertEqual(len(config.accounts), 1)

    def test_top_level_not_dict_raises(self) -> None:
        with self.assertRaises(ConfigError):
            parse_config([1, 2, 3])

    def test_accounts_not_list_raises(self) -> None:
        with self.assertRaises(ConfigError):
            parse_config({"config_version": 2, "accounts": "not-a-list"})

    def test_account_not_object_raises(self) -> None:
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "config_version": 2,
                    "accounts": ["not-a-dict"],
                }
            )

    def test_imap_account_not_object_raises(self) -> None:
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "config_version": 2,
                    "accounts": ["not-a-dict"],
                }
            )

    def test_password_command_not_list_raises(self) -> None:
        data = sample_config()
        data["accounts"][0]["password_command"] = "a-string-not-a-list"  # type: ignore[index]
        with self.assertRaises(ConfigError):
            parse_config(data)

    def test_config_version_not_int_raises(self) -> None:
        with self.assertRaises(ConfigError):
            parse_config({"config_version": "two"})

    def test_trash_retention_days_negative_raises(self) -> None:
        data = sample_config()
        data["accounts"][0]["mirror"]["trash_retention_days"] = -1  # type: ignore[index]
        with self.assertRaises(ConfigError):
            parse_config(data)

    def test_require_mapping_not_dict_raises(self) -> None:
        data = sample_config()
        data["accounts"][0]["smtp"] = "not-a-dict"  # type: ignore[index]
        with self.assertRaises(ConfigError):
            parse_config(data)

    def test_require_string_list_not_list_raises(self) -> None:
        data = sample_config()
        data["accounts"][0]["folders"] = {
            "include": "not-a-list",
            "exclude": [],
            "read_only": [],
        }  # type: ignore[index]
        with self.assertRaises(ConfigError):
            parse_config(data)

    def test_require_int_not_int_raises(self) -> None:
        data = sample_config()
        data["accounts"][0]["imap_port"] = "not-an-int"  # type: ignore[index]
        with self.assertRaises(ConfigError):
            parse_config(data)

    def test_optional_string_empty_raises(self) -> None:
        data = sample_config()
        data["accounts"][0]["sent_folder"] = ""  # type: ignore[index]
        with self.assertRaises(ConfigError):
            parse_config(data)

    def test_require_bool_not_bool_raises(self) -> None:
        data = sample_config()
        data["accounts"][0]["imap_ssl"] = "true"  # type: ignore[index]
        with self.assertRaises(ConfigError):
            parse_config(data)

    def test_invalid_credentials_source_raises(self) -> None:
        data = sample_config()
        data["accounts"][0]["credentials_source"] = "magic"  # type: ignore[index]
        with self.assertRaises(ConfigError):
            parse_config(data)


class FolderPolicyTestCase(unittest.TestCase):
    """Validate FolderConfig.should_sync and is_read_only semantics."""

    def _policy(
        self,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        read_only: list[str] | None = None,
    ) -> FolderConfig:
        return FolderConfig(
            include=tuple(include or []),
            exclude=tuple(exclude or []),
            read_only=tuple(read_only or []),
        )

    def test_empty_policy_syncs_all(self) -> None:
        p = self._policy()
        self.assertTrue(p.should_sync("INBOX"))
        self.assertTrue(p.should_sync("Sent"))
        self.assertFalse(p.is_read_only("INBOX"))

    def test_include_alone_does_not_restrict(self) -> None:
        # include is exceptions to exclude, not a global whitelist: folders
        # matched by neither list still sync.
        p = self._policy(include=["INBOX", "Archive"])
        self.assertTrue(p.should_sync("INBOX"))
        self.assertTrue(p.should_sync("Sent"))

    def test_include_rescues_subfolders_from_exclude(self) -> None:
        p = self._policy(include=["Archive/.*"], exclude=["Archive.*"])
        self.assertTrue(p.should_sync("Archive/2024"))  # rescued by include
        self.assertFalse(p.should_sync("Archive"))  # excluded, not rescued
        self.assertTrue(p.should_sync("INBOX"))  # untouched -> syncs

    def test_include_overrides_exclude(self) -> None:
        p = self._policy(include=["INBOX", "Spam"], exclude=["Spam"])
        self.assertTrue(p.should_sync("INBOX"))
        self.assertTrue(p.should_sync("Spam"))  # include wins

    def test_exclude_all_then_include_whitelist(self) -> None:
        p = self._policy(exclude=[".*"], include=["INBOX", "Sent"])
        self.assertTrue(p.should_sync("INBOX"))
        self.assertTrue(p.should_sync("Sent"))
        self.assertFalse(p.should_sync("Trash"))
        self.assertFalse(p.should_sync("Spam"))

    def test_exclude_regex_wildcard(self) -> None:
        p = self._policy(exclude=[r"\[Gmail\]/.*"])  # raw string fine in Python
        self.assertFalse(p.should_sync("[Gmail]/All Mail"))
        self.assertFalse(p.should_sync("[Gmail]/Spam"))
        self.assertTrue(p.should_sync("INBOX"))

    def test_read_only_bypasses_include_filter(self) -> None:
        p = self._policy(include=["INBOX"], read_only=["Sent"])
        self.assertTrue(p.should_sync("Sent"))
        self.assertTrue(p.is_read_only("Sent"))

    def test_read_only_wildcard_matches_all(self) -> None:
        p = self._policy(read_only=[".*"])
        self.assertTrue(p.is_read_only("INBOX"))
        self.assertTrue(p.is_read_only("Sent"))
        self.assertTrue(p.is_read_only("[Gmail]/All Mail"))

    def test_read_only_overrides_exclude(self) -> None:
        p = self._policy(read_only=["Sent"], exclude=["Sent"])
        self.assertTrue(p.should_sync("Sent"))  # read_only wins

    def test_is_read_only_false_for_normal_folder(self) -> None:
        p = self._policy(read_only=["Sent"])
        self.assertFalse(p.is_read_only("INBOX"))


def sample_config() -> dict[str, object]:
    """Return a minimal valid application config."""
    return {
        "config_version": 2,
        "accounts": [
            {
                "name": "personal",
                "email_address": "user@example.com",
                "imap_host": "imap.example.com",
                "smtp": {"host": "smtp.example.com"},
                "username": "user",
                "credentials_source": "plaintext",
                "mirror": {
                    "path": "mirrors/personal",
                    "format": "maildir",
                    "trash_retention_days": 30,
                },
            },
        ],
    }


def sample_toml_config() -> str:
    """Return a minimal valid TOML app configuration."""
    return """
config_version = 2

[[accounts]]
name = "personal"
email_address = "user@example.com"
imap_host = "imap.example.com"
username = "user"
credentials_source = "plaintext"

[accounts.smtp]
host = "smtp.example.com"

[accounts.mirror]
path = "mirrors/personal"
format = "maildir"
trash_retention_days = 30
""".strip()
