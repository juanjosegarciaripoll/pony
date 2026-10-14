from __future__ import annotations

import tempfile
import tomllib
import unittest
from collections.abc import Callable
from pathlib import Path

from pony.calendar.bootstrap import (
    TEMPLATE_TOML,
    write_template,
)
from pony.calendar.config import load as load_config


class TemplateContentTest(unittest.TestCase):
    """The template is the first thing a user sees; assert its shape."""

    def test_parses_as_toml_with_no_active_accounts(self) -> None:
        data = tomllib.loads(TEMPLATE_TOML.decode())
        self.assertEqual(data["config_version"], 1)
        # All example accounts are commented out, so a freshly-written
        # template parses to zero accounts.
        self.assertNotIn("accounts", data)

    def test_loads_through_the_calendars_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            write_template(path)
            config = load_config(path)
        self.assertEqual(config.config_version, 1)
        self.assertEqual(config.accounts, ())

    def test_includes_both_basic_and_oauth_examples(self) -> None:
        body = TEMPLATE_TOML.decode()
        self.assertIn('backend = "plaintext"', body)
        self.assertIn('backend = "oauth"', body)
        self.assertIn("Nextcloud", body)
        self.assertIn("Google", body)


class WriteTemplateTest(unittest.TestCase):
    def test_creates_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "deep" / "deeper" / "config.toml"
            write_template(target)
            self.assertTrue(target.exists())
            self.assertEqual(target.read_bytes(), TEMPLATE_TOML)

    def test_overwrites_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.toml"
            target.write_bytes(b"old contents")
            write_template(target)
            self.assertEqual(target.read_bytes(), TEMPLATE_TOML)


def _scripted_prompt(answers: list[str]) -> Callable[[str], str]:
    """Return a prompt function that pops successive canned answers."""
    iterator = iter(answers)

    def prompt(_message: str) -> str:
        try:
            return next(iterator)
        except StopIteration as exc:
            raise AssertionError("prompt called more times than expected") from exc

    return prompt
