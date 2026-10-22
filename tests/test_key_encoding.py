"""The composer's Alt shortcuts, checked against real terminal bytes.

``Pilot.press("alt+a")`` injects a key *name*, so no TUI test can see a
change in the layer that turns terminal bytes into those names.  That
layer broke once already: Textual 8.2.7 began asking terminals for
kitty's "report associated text" enhancement and then discarded the alt
modifier from every key event carrying text, so Windows Terminal's
``CSI 97;3;97u`` for Alt+A arrived as a bare ``a`` and the composer
typed the letter into the message instead of opening the file picker.

These tests parse the byte sequences a terminal really sends, so a
dependency bump that unbinds the composer's keys fails here rather than
in a user's mailbox.
"""

from __future__ import annotations

import inspect
import re
import unittest

from textual._xterm_parser import XTermParser  # noqa: PLC2701
from textual.binding import Binding
from textual.drivers import linux_driver
from textual.events import Key

from pony.tui.screens.compose_screen import ComposeScreen


def _keys(sequence: str) -> list[str]:
    """Return the key names Textual derives from *sequence*.

    A parser is single-use: feeding ``""`` closes it, which is how the
    pending-escape timeout is resolved.
    """
    parser = XTermParser()
    events = [*parser.feed(sequence), *parser.feed("")]
    return [event.key for event in events if isinstance(event, Key)]


def _alt_letter_bindings() -> dict[str, str]:
    """Map every ``alt+<letter>`` binding of the composer to its letter."""
    return {
        binding.key: binding.key.removeprefix("alt+")
        for binding in ComposeScreen.BINDINGS
        if isinstance(binding, Binding) and re.fullmatch(r"alt\+[a-z]", binding.key)
    }


def _alt_shift_letter_bindings() -> dict[str, str]:
    """Map every ``alt+shift+<letter>`` binding to its letter."""
    return {
        binding.key: binding.key.removeprefix("alt+shift+")
        for binding in ComposeScreen.BINDINGS
        if isinstance(binding, Binding)
        and re.fullmatch(r"alt\+shift\+[a-z]", binding.key)
    }


def _requests_associated_text() -> bool:
    """Does the Linux driver ask terminals to embed text in key events?

    This reads the driver's source on purpose: the handshake it writes
    decides which of the encodings below a terminal will use, and there
    is no public accessor for it.
    """
    source = inspect.getsource(linux_driver)
    match = re.search(r"KITTY_PROTOCOL_FLAG\s*=\s*\(([^)]*)\)", source)
    if match is None:
        return False
    return "KITTY_REPORT_ASSOCIATED_TEXT" in match.group(1)


class ComposerAltBindingTests(unittest.TestCase):
    """The composer binds keys that terminals can actually deliver."""

    def test_there_are_alt_letter_bindings_to_check(self) -> None:
        self.assertTrue(_alt_letter_bindings())

    def test_every_alt_letter_has_a_shifted_twin(self) -> None:
        """Caps Lock, or a held shift, must not type the letter instead."""
        shifted = set(_alt_shift_letter_bindings().values())
        for key, letter in _alt_letter_bindings().items():
            with self.subTest(key=key):
                self.assertIn(letter, shifted)


class AltKeyEncodingTests(unittest.TestCase):
    """Each encoding a terminal may use must yield the bound key name."""

    def test_legacy_escape_prefix(self) -> None:
        """Without the kitty protocol, Alt+A is sent as ``ESC a``."""
        for key, letter in _alt_letter_bindings().items():
            with self.subTest(key=key):
                self.assertEqual(_keys(f"\x1b{letter}"), [key])

    def test_legacy_escape_prefix_with_shift(self) -> None:
        """``ESC A`` — Caps Lock or a held shift — is a distinct key."""
        for key, letter in _alt_shift_letter_bindings().items():
            with self.subTest(key=key):
                self.assertEqual(_keys(f"\x1b{letter.upper()}"), [key])

    def test_kitty_disambiguated_escape_codes(self) -> None:
        """With flag 1 only, Alt+A is ``CSI 97;3u``."""
        for key, letter in _alt_letter_bindings().items():
            with self.subTest(key=key):
                self.assertEqual(_keys(f"\x1b[{ord(letter)};3u"), [key])

    def test_kitty_associated_text_keeps_the_modifier(self) -> None:
        """With flag 16, the text must not cost us the alt modifier.

        Terminals answering "report associated text" send the modifier
        *and* the text — ``CSI 97;3;97u`` for Alt+A.  A reader that
        drops the modifier because text is present unbinds every
        shortcut here; one that keeps it loses nothing, since alt is
        only genuinely consumed when the text differs from the key's
        own codepoint (macOS Option+A yielding "å").
        """
        if not _requests_associated_text():
            self.skipTest("driver does not request kitty associated text")
        for key, letter in _alt_letter_bindings().items():
            with self.subTest(key=key):
                code = ord(letter)
                self.assertEqual(_keys(f"\x1b[{code};3;{code}u"), [key])
