from __future__ import annotations

import re
import tempfile
import textwrap
import tomllib
import unittest
import unittest.mock
from pathlib import Path

from pony.calendar.config import ConfigError, dump, load, mirror_roots, parse, save
from pony.calendar.domain import (
    AccountConfig,
    AppConfig,
    CommandCredential,
    EnvCredential,
    PlaintextCredential,
)
from pony.calendar.paths import CalendarPaths


def _toml(body: str) -> str:
    return textwrap.dedent(body).strip() + "\n"


class LoadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _write(self, content: str) -> Path:
        path = self.tmp / "config.toml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_load_missing_file(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            load(self.tmp / "nonexistent.toml")
        self.assertIn("not found", str(ctx.exception))

    def test_load_invalid_toml(self) -> None:
        path = self._write("this is = = not valid")
        with self.assertRaises(ConfigError) as ctx:
            load(path)
        self.assertIn("TOML parse error", str(ctx.exception))

    def test_load_minimal_config(self) -> None:
        path = self._write(
            _toml("""
            config_version = 1
        """)
        )
        config = load(path)
        self.assertEqual(config.config_version, 1)
        self.assertFalse(config.use_utf8)
        self.assertIsNone(config.editor)
        self.assertEqual(config.accounts, ())


class ParseTopLevelTest(unittest.TestCase):
    def test_requires_config_version(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            parse({})
        self.assertIn("config_version", str(ctx.exception))

    def test_an_unsupported_config_version_is_refused(self) -> None:
        """The field was read and never compared with anything.

        `config_version = 999` parsed cleanly and the file was then
        interpreted as the current schema, which is the one thing the
        number exists to prevent.  The mail half has had the strict
        equality since its first commit.
        """
        with self.assertRaises(ConfigError) as ctx:
            parse({"config_version": 999})
        self.assertIn("unsupported config_version 999", str(ctx.exception))

    def test_config_version_must_be_int(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            parse({"config_version": "1"})
        self.assertIn("config_version", str(ctx.exception))
        self.assertIn("integer", str(ctx.exception))

    def test_use_utf8_rejects_non_bool(self) -> None:
        with self.assertRaises(ConfigError):
            parse({"config_version": 1, "use_utf8": "yes"})

    def test_use_utf8_and_editor_parsed(self) -> None:
        config = parse({"config_version": 1, "use_utf8": True, "editor": "nvim"})
        self.assertTrue(config.use_utf8)
        self.assertEqual(config.editor, "nvim")

    def test_accounts_must_be_list(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            parse({"config_version": 1, "accounts": "not a list"})
        self.assertIn("accounts", ctx.exception.key)

    def test_theme_defaults_to_none_and_round_trips(self) -> None:
        without = parse({"config_version": 1})
        self.assertIsNone(without.theme)
        # Absent theme stays out of the serialised form.
        self.assertNotIn("theme", dump(without))

        with_theme = parse({"config_version": 1, "theme": "nord"})
        self.assertEqual(with_theme.theme, "nord")
        # parse(dump(c)) == c round-trip invariant holds for theme.
        self.assertEqual(parse(dump(with_theme)), with_theme)
        self.assertEqual(dump(with_theme)["theme"], "nord")

    def test_background_sync_defaults_to_hourly_and_round_trips(self) -> None:
        default = parse({"config_version": 1})
        self.assertTrue(default.background_sync_enabled)
        self.assertEqual(default.background_sync_interval_seconds, 3600)
        self.assertNotIn("background_sync_enabled", dump(default))
        self.assertNotIn("background_sync_interval_seconds", dump(default))

        custom = parse(
            {
                "config_version": 1,
                "background_sync_enabled": False,
                "background_sync_interval_seconds": 600,
            }
        )
        self.assertFalse(custom.background_sync_enabled)
        self.assertEqual(custom.background_sync_interval_seconds, 600)
        self.assertEqual(parse(dump(custom)), custom)

    def test_background_sync_interval_must_be_positive(self) -> None:
        with self.assertRaises(ConfigError):
            parse({"config_version": 1, "background_sync_interval_seconds": 0})


class ParseAccountTest(unittest.TestCase):
    BASE_ACCOUNT: dict[str, object] = {
        "name": "personal",
        "url": "https://caldav.example.com/dav/",
        "username": "user@example.com",
        "credential": {"backend": "env", "variable": "CHRONOS_PERSONAL_PW"},
        "mirror_path": "/tmp/pony/calendar/personal",
    }

    def _wrap(self, account: dict[str, object]) -> dict[str, object]:
        return {"config_version": 1, "accounts": [account]}

    def test_minimal_valid_account(self) -> None:
        config = parse(self._wrap(dict(self.BASE_ACCOUNT)))
        self.assertEqual(len(config.accounts), 1)
        acct = config.accounts[0]
        self.assertEqual(acct.name, "personal")
        self.assertEqual(acct.url, "https://caldav.example.com/dav/")
        self.assertEqual(acct.username, "user@example.com")
        self.assertIsInstance(acct.credential, EnvCredential)
        self.assertEqual(acct.trash_retention_days, 30)
        self.assertEqual(len(acct.include), 1)
        self.assertEqual(acct.include[0].pattern, ".*")
        self.assertEqual(acct.exclude, ())
        self.assertEqual(acct.read_only, ())

    def test_missing_required_field(self) -> None:
        bad = dict(self.BASE_ACCOUNT)
        del bad["username"]
        with self.assertRaises(ConfigError) as ctx:
            parse(self._wrap(bad))
        self.assertIn("username", str(ctx.exception))

    def test_mirror_path_is_expanded(self) -> None:
        acct_data = dict(self.BASE_ACCOUNT)
        acct_data["mirror_path"] = "~/chronos-test-mirror"
        config = parse(self._wrap(acct_data))
        path = config.accounts[0].mirror_path
        self.assertNotIn("~", str(path))
        self.assertTrue(path.is_absolute())

    def test_a_relative_mirror_path_resolves_under_the_calendar_root(self) -> None:
        """Not under the working directory the command happened to run in."""
        acct_data = dict(self.BASE_ACCOUNT)
        acct_data["mirror_path"] = "mirrors/personal"
        config = parse(self._wrap(acct_data))

        self.assertEqual(
            mirror_roots(config, self.calendar_paths)["personal"],
            (self.calendar_paths.root / "mirrors" / "personal").resolve(),
        )

    def test_a_relative_mirror_path_is_resolved_against_a_given_root(self) -> None:
        """Every later use of the path is a spelling comparison.

        The sync engine compares it with what it wrote, and an
        unresolved ``a/../b`` is a different string for the same
        directory.  The resolving happens where a root is at hand --
        parsing only keeps what the file said, because the root is the
        host's to name and used to come from a process-wide global.
        """
        acct_data = dict(self.BASE_ACCOUNT)
        acct_data["mirror_path"] = "mirrors/../elsewhere"
        config = parse(self._wrap(acct_data))

        self.assertEqual(config.accounts[0].mirror_path, Path("mirrors/../elsewhere"))
        resolved = mirror_roots(config, self.calendar_paths)["personal"]
        self.assertEqual(resolved, (self.calendar_paths.root / "elsewhere").resolve())

    def test_the_same_file_resolves_under_whichever_root_it_is_given(self) -> None:
        """What a configuration *means* no longer depends on process state.

        The parser read the calendar's root from a module global, so
        two roots in one process -- which is what the two conftests of
        this repository set up -- made the same file name different
        directories depending on which had been activated last.
        """
        acct_data = dict(self.BASE_ACCOUNT)
        acct_data["mirror_path"] = "mirrors/personal"
        config = parse(self._wrap(acct_data))

        one = CalendarPaths(root=Path("/one"))
        two = CalendarPaths(root=Path("/two"))

        self.assertEqual(
            mirror_roots(config, one)["personal"], Path("/one/mirrors/personal")
        )
        self.assertEqual(
            mirror_roots(config, two)["personal"], Path("/two/mirrors/personal")
        )

    def test_a_half_qualified_mirror_path_is_refused(self) -> None:
        """A root without a drive, or a drive without a root.

        Neither is absolute, so joining it against the calendar's root
        discards the root: "/mail" becomes C:\\mail.  The mail side
        refuses that shape for `mirror.path`; the calendar joined it.
        Only a Windows path can have it, so the predicate is asked
        directly rather than through a path this machine cannot spell.
        """
        acct_data = dict(self.BASE_ACCOUNT)
        acct_data["mirror_path"] = "mirrors/personal"
        with (
            unittest.mock.patch(
                "pony.calendar.config.is_half_qualified", return_value=True
            ),
            self.assertRaises(ConfigError) as ctx,
        ):
            parse(self._wrap(acct_data))
        self.assertIn("neither relative nor fully qualified", str(ctx.exception))

    def test_an_absolute_mirror_path_is_its_own_answer(self) -> None:
        """A root is for resolving relative paths, not for overriding one."""
        acct_data = dict(self.BASE_ACCOUNT)
        acct_data["mirror_path"] = "/srv/calendars/personal"
        config = parse(self._wrap(acct_data))

        self.assertEqual(
            mirror_roots(config, CalendarPaths(root=Path("/elsewhere")))["personal"],
            Path("/srv/calendars/personal"),
        )

    def test_an_omitted_mirror_path_declares_nothing(self) -> None:
        acct_data = dict(self.BASE_ACCOUNT)
        del acct_data["mirror_path"]
        config = parse(self._wrap(acct_data))

        self.assertIsNone(config.accounts[0].mirror_path)
        # And takes the layout's default wherever it is resolved.
        self.assertEqual(
            mirror_roots(config, self.calendar_paths)["personal"],
            self.calendar_paths.mirror_path("personal"),
        )

    def test_mirror_path_must_be_a_string_when_present(self) -> None:
        acct_data = dict(self.BASE_ACCOUNT)
        acct_data["mirror_path"] = 42
        with self.assertRaises(ConfigError) as ctx:
            parse(self._wrap(acct_data))
        self.assertIn("mirror_path", str(ctx.exception))
        self.assertIn("string", str(ctx.exception))

    def test_include_regex_compiled(self) -> None:
        acct_data = dict(self.BASE_ACCOUNT)
        acct_data["include"] = ["^personal-.*$", "^shared$"]
        config = parse(self._wrap(acct_data))
        patterns = config.accounts[0].include
        self.assertEqual(len(patterns), 2)
        self.assertIsNotNone(patterns[0].fullmatch("personal-work"))
        self.assertIsNone(patterns[0].fullmatch("other"))

    def test_invalid_regex_surfaces_key(self) -> None:
        acct_data = dict(self.BASE_ACCOUNT)
        acct_data["include"] = ["["]
        with self.assertRaises(ConfigError) as ctx:
            parse(self._wrap(acct_data))
        self.assertIn("include", ctx.exception.key)

    def test_google_account_omits_url_and_username(self) -> None:
        from pony.calendar.domain import GOOGLE_CALDAV_URL, GoogleCredential

        config = parse(
            {
                "config_version": 1,
                "accounts": [
                    {
                        "name": "google",
                        "credential": {
                            "backend": "google",
                            "client_id": "cid.apps.googleusercontent.com",
                            "client_secret": "GOCSPX-x",
                        },
                    }
                ],
            }
        )
        acct = config.accounts[0]
        self.assertIsInstance(acct.credential, GoogleCredential)
        self.assertEqual(acct.url, GOOGLE_CALDAV_URL)
        self.assertEqual(acct.username, "")

    def test_non_google_account_still_requires_url(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            parse(
                {
                    "config_version": 1,
                    "accounts": [
                        {
                            "name": "personal",
                            "username": "u@example.com",
                            "credential": {"backend": "env", "variable": "X"},
                        }
                    ],
                }
            )
        self.assertIn("url", str(ctx.exception))

    def test_trash_retention_days_override(self) -> None:
        acct_data = dict(self.BASE_ACCOUNT)
        acct_data["trash_retention_days"] = 7
        config = parse(self._wrap(acct_data))
        self.assertEqual(config.accounts[0].trash_retention_days, 7)


class ParseCredentialTest(unittest.TestCase):
    def _wrap(self, credential: object) -> dict[str, object]:
        return {
            "config_version": 1,
            "accounts": [
                {
                    "name": "a",
                    "url": "https://caldav.example.com/",
                    "username": "u@example.com",
                    "credential": credential,
                    "mirror_path": "/tmp/m",
                }
            ],
        }

    def test_plaintext(self) -> None:
        config = parse(self._wrap({"backend": "plaintext", "password": "s3cret"}))
        cred = config.accounts[0].credential
        self.assertIsInstance(cred, PlaintextCredential)
        assert isinstance(cred, PlaintextCredential)
        self.assertEqual(cred.password, "s3cret")

    def test_env(self) -> None:
        config = parse(self._wrap({"backend": "env", "variable": "X"}))
        cred = config.accounts[0].credential
        assert isinstance(cred, EnvCredential)
        self.assertEqual(cred.variable, "X")

    def test_command(self) -> None:
        config = parse(
            self._wrap({"backend": "command", "command": ["pass", "show", "x"]})
        )
        cred = config.accounts[0].credential
        assert isinstance(cred, CommandCredential)
        self.assertEqual(cred.command, ("pass", "show", "x"))

    def test_command_empty_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            parse(self._wrap({"backend": "command", "command": []}))

    def test_encrypted_is_refused_at_parse(self) -> None:
        """The word that works for mail and not for the calendar.

        A mail account's ``credentials_source = "encrypted"`` is the
        DPAPI/SHAKE store in `pony.mail.credentials`.  The same word on a
        calendar account used to parse into a spec no resolver could
        answer, so the config loaded and the first sync failed; the
        refusal belongs where the word is read, and it names what does
        work instead.
        """
        with self.assertRaises(ConfigError) as ctx:
            parse(
                self._wrap(
                    {"backend": "encrypted", "service": "chronos", "username": "u"}
                )
            )
        message = str(ctx.exception)
        self.assertIn("'encrypted' backend is not available", message)
        self.assertIn("'command'", message)

    def test_oauth_minimal(self) -> None:
        config = parse(
            self._wrap(
                {
                    "backend": "oauth",
                    "client_id": "1234.apps.googleusercontent.com",
                    "client_secret": "secret-value",
                }
            )
        )
        from pony.calendar.domain import OAuthCredential

        cred = config.accounts[0].credential
        assert isinstance(cred, OAuthCredential)
        self.assertEqual(cred.client_id, "1234.apps.googleusercontent.com")
        self.assertEqual(cred.client_secret, "secret-value")
        self.assertEqual(cred.scope, "https://www.googleapis.com/auth/calendar")
        self.assertIsNone(cred.token_path)

    def test_oauth_with_custom_scope_and_token_path(self) -> None:
        config = parse(
            self._wrap(
                {
                    "backend": "oauth",
                    "client_id": "cid",
                    "client_secret": "cs",
                    "scope": "https://example/scope",
                    "token_path": "/tmp/mytokens.json",
                }
            )
        )
        from pony.calendar.domain import OAuthCredential

        cred = config.accounts[0].credential
        assert isinstance(cred, OAuthCredential)
        self.assertEqual(cred.scope, "https://example/scope")
        self.assertEqual(cred.token_path, Path("/tmp/mytokens.json"))

    def test_oauth_missing_client_id_raises(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            parse(self._wrap({"backend": "oauth", "client_secret": "cs"}))
        self.assertIn("client_id", str(ctx.exception))

    def test_google_minimal(self) -> None:
        config = parse(
            self._wrap(
                {
                    "backend": "google",
                    "client_id": "1234.apps.googleusercontent.com",
                    "client_secret": "GOCSPX-secret",
                }
            )
        )
        from pony.calendar.domain import GoogleCredential

        cred = config.accounts[0].credential
        assert isinstance(cred, GoogleCredential)
        self.assertEqual(cred.client_id, "1234.apps.googleusercontent.com")
        self.assertEqual(cred.client_secret, "GOCSPX-secret")

    def test_google_missing_client_id_raises(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            parse(self._wrap({"backend": "google", "client_secret": "cs"}))
        self.assertIn("client_id", str(ctx.exception))

    def test_unknown_backend(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            parse(self._wrap({"backend": "saml2"}))
        self.assertIn("unknown credential backend", str(ctx.exception))

    def test_missing_credential(self) -> None:
        with self.assertRaises(ConfigError):
            parse(
                {
                    "config_version": 1,
                    "accounts": [
                        {
                            "name": "a",
                            "url": "https://x/",
                            "username": "u@example.com",
                            "mirror_path": "/tmp/m",
                        }
                    ],
                }
            )


def _sample_config(**overrides: object) -> AppConfig:
    account = AccountConfig(
        name=str(overrides.get("name", "personal")),
        url="https://caldav.example.com/dav/",
        username="user@example.com",
        credential=EnvCredential(variable="CHRONOS_PERSONAL_PW"),
        mirror_path=Path("/tmp/pony/calendar/personal"),
        trash_retention_days=30,
        include=(re.compile(".*"),),
        exclude=(),
        read_only=(),
    )
    return AppConfig(
        config_version=1,
        use_utf8=False,
        editor=None,
        accounts=(account,),
    )


class DumpTest(unittest.TestCase):
    def test_dump_produces_valid_toml(self) -> None:
        from pony.calendar.config import _dumps_toml  # noqa: PLC0415

        config = _sample_config()
        rendered = _dumps_toml(dump(config))
        # Round-trip through tomllib to prove the output is valid TOML.
        reparsed = tomllib.loads(rendered)
        self.assertEqual(reparsed["config_version"], 1)
        self.assertEqual(len(reparsed["accounts"]), 1)
        self.assertEqual(reparsed["accounts"][0]["name"], "personal")

    def test_dump_omits_none_editor(self) -> None:
        data = dump(_sample_config())
        self.assertNotIn("editor", data)

    def test_dump_omits_mirror_path_when_default(self) -> None:
        config = _sample_config()
        config = AppConfig(
            config_version=config.config_version,
            use_utf8=config.use_utf8,
            editor=config.editor,
            accounts=(
                AccountConfig(
                    name=config.accounts[0].name,
                    url=config.accounts[0].url,
                    username=config.accounts[0].username,
                    credential=config.accounts[0].credential,
                    mirror_path=None,
                    trash_retention_days=config.accounts[0].trash_retention_days,
                    include=config.accounts[0].include,
                    exclude=config.accounts[0].exclude,
                    read_only=config.accounts[0].read_only,
                ),
            ),
        )
        data = dump(config)
        accounts = data["accounts"]
        assert isinstance(accounts, list)
        self.assertNotIn("mirror_path", accounts[0])
        # And the round-trip keeps it on the default.
        reparsed = parse(data)
        self.assertIsNone(reparsed.accounts[0].mirror_path)

    def test_dump_omits_url_and_username_for_google_defaults(self) -> None:
        from pony.calendar.domain import GoogleCredential

        account = AccountConfig(
            name="google",
            url="https://apidata.googleusercontent.com/caldav/v2/",
            username="",
            credential=GoogleCredential(client_id="cid", client_secret="cs"),
            mirror_path=None,
            trash_retention_days=30,
            include=(re.compile(".*"),),
            exclude=(),
            read_only=(),
        )
        config = AppConfig(
            config_version=1, use_utf8=False, editor=None, accounts=(account,)
        )
        data = dump(config)
        from typing import cast as _cast

        accounts = _cast(list[dict[str, object]], data["accounts"])
        self.assertNotIn("url", accounts[0])
        self.assertNotIn("username", accounts[0])
        # Round-trip recovers the same account from the minimal form.
        reparsed = parse(data)
        self.assertEqual(reparsed.accounts[0], account)

    def test_dump_keeps_mirror_path_when_custom(self) -> None:
        config = _sample_config()
        # _sample_config uses /tmp/pony/calendar/personal which is NOT the
        # platform default; dump should keep it. Path serialisation is
        # platform-specific (str() of a WindowsPath uses backslashes),
        # so compare via Path equality.
        data = dump(config)
        accounts = data["accounts"]
        assert isinstance(accounts, list)
        self.assertEqual(
            Path(str(accounts[0]["mirror_path"])), Path("/tmp/pony/calendar/personal")
        )

    def test_dump_includes_editor_when_set(self) -> None:
        config = _sample_config()
        config = AppConfig(
            config_version=config.config_version,
            use_utf8=config.use_utf8,
            editor="nvim",
            accounts=config.accounts,
        )
        data = dump(config)
        self.assertEqual(data["editor"], "nvim")

    def test_credential_roundtrip(self) -> None:
        from pony.calendar.domain import GoogleCredential, OAuthCredential

        for cred in (
            PlaintextCredential(password="s3cret"),
            EnvCredential(variable="MY_VAR"),
            CommandCredential(command=("pass", "show", "chronos")),
            OAuthCredential(
                client_id="cid",
                client_secret="cs",
                scope="https://example/scope",
            ),
            OAuthCredential(
                client_id="cid2",
                client_secret="cs2",
                scope="https://example/scope",
                token_path=Path("/tmp/t.json"),
            ),
            GoogleCredential(
                client_id="g.apps.googleusercontent.com",
                client_secret="GOCSPX-x",
            ),
        ):
            with self.subTest(backend=type(cred).__name__):
                config = _sample_config()
                config = AppConfig(
                    config_version=config.config_version,
                    use_utf8=config.use_utf8,
                    editor=None,
                    accounts=(
                        AccountConfig(
                            name=config.accounts[0].name,
                            url=config.accounts[0].url,
                            username=config.accounts[0].username,
                            credential=cred,
                            mirror_path=config.accounts[0].mirror_path,
                            trash_retention_days=config.accounts[
                                0
                            ].trash_retention_days,
                            include=config.accounts[0].include,
                            exclude=config.accounts[0].exclude,
                            read_only=config.accounts[0].read_only,
                        ),
                    ),
                )
                reparsed = parse(dump(config))
                self.assertEqual(reparsed.accounts[0].credential, cred)


class SaveRoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_save_then_load_produces_equal_config(self) -> None:
        original = _sample_config()
        path = self.tmp / "config.toml"
        save(original, path)
        self.assertTrue(path.exists())
        loaded = load(path)
        # Regex patterns aren't directly comparable -- check .pattern.
        self.assertEqual(loaded.config_version, original.config_version)
        self.assertEqual(loaded.use_utf8, original.use_utf8)
        self.assertEqual(loaded.editor, original.editor)
        self.assertEqual(len(loaded.accounts), 1)
        orig_a = original.accounts[0]
        load_a = loaded.accounts[0]
        self.assertEqual(load_a.name, orig_a.name)
        self.assertEqual(load_a.url, orig_a.url)
        self.assertEqual(load_a.username, orig_a.username)
        self.assertEqual(load_a.credential, orig_a.credential)
        self.assertEqual(load_a.trash_retention_days, orig_a.trash_retention_days)
        self.assertEqual(
            [p.pattern for p in load_a.include],
            [p.pattern for p in orig_a.include],
        )

    def test_save_creates_parent_directory(self) -> None:
        path = self.tmp / "nested" / "deep" / "config.toml"
        save(_sample_config(), path)
        self.assertTrue(path.exists())

    def test_save_is_atomic_no_tempfile_leftovers(self) -> None:
        path = self.tmp / "config.toml"
        save(_sample_config(), path)
        leftovers = [p for p in self.tmp.iterdir() if p.name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_save_keyboard_interrupt_preserves_prior_file(self) -> None:
        # Mid-save Ctrl-C must leave the previous config intact and
        # not strand a `.tmp-*` file. Otherwise the user's next
        # `pony calendar sync` could find no config or a half-written one.
        path = self.tmp / "config.toml"
        save(_sample_config(name="original-account"), path)
        original_bytes = path.read_bytes()
        with (
            unittest.mock.patch(
                "pony.atomic.os.replace", side_effect=KeyboardInterrupt
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            save(_sample_config(name="overwriting-account"), path)
        self.assertEqual(path.read_bytes(), original_bytes)
        leftovers = [p for p in self.tmp.iterdir() if p.name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])
