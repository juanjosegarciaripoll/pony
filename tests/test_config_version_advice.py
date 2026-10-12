"""Every config_version Pony states is the one its loader accepts.

``CONFIG_VERSION`` went from 2 to 3 and two hand-written copies stayed
behind: the block ``pony account add`` prints when it has no terminal,
and the example in :mod:`pony.calendar`'s own docstring.  The file Pony
*writes* was right because it interpolates the constant; the advice it
*printed* told a user to hand-write a config the loader then refused.

So the test is not "does it say 3" -- it is that nothing anywhere states
a version by hand, which is the mistake rather than the number.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from tui_helpers import make_tmp_paths

from pony.config import ConfigError
from pony.domain import CONFIG_VERSION

if TYPE_CHECKING:
    import pytest

# Any `config_version = <digits>`, however it is spelled around.
_STATED = re.compile(r"config_version\s*=\s*(\d+)")


def _stated_versions(text: str) -> set[int]:
    return {int(match) for match in _STATED.findall(text)}


def test_the_account_template_quotes_the_version_the_loader_wants(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from pony.cli import run_account_add

    paths = make_tmp_paths("config-version-advice")

    with patch("sys.stdin.isatty", return_value=False):
        code = run_account_add(
            paths=paths,
            config_path=None,
            account_name="work",
        )

    assert code == 0
    printed = capsys.readouterr().out
    assert _stated_versions(printed) == {CONFIG_VERSION}


def test_the_calendar_sections_documented_example_does_too() -> None:
    import pony.calendar

    assert pony.calendar.__doc__ is not None
    assert _stated_versions(pony.calendar.__doc__) == {CONFIG_VERSION}


def test_the_sample_config_agrees_with_the_constant() -> None:
    """Rule 6: ``config-sample.toml`` is half of the config contract."""
    sample = Path(__file__).resolve().parents[1] / "config-sample.toml"

    assert _stated_versions(sample.read_text(encoding="utf-8")) == {CONFIG_VERSION}


def test_the_changelog_the_refusal_points_at_has_the_notes() -> None:
    """The refusal sends the reader to the CHANGELOG, so they must be there.

    ``_require_config_version`` tells a user with an older file to
    "update your config.toml to the current schema (see the sample
    config and CHANGELOG for migration notes)" -- and the release that
    required the new schema listed none.  The notes are an
    ``### Upgrading`` subsection under the newest release, as ``1.0.0``
    has, and they have to name every key an older config is missing and
    every command whose behaviour moved with it.
    """
    from pony.config import _require_config_version

    root = Path(__file__).resolve().parents[1]
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")

    try:
        _require_config_version({"config_version": CONFIG_VERSION - 1})
    except ConfigError as refusal:
        assert "CHANGELOG" in str(refusal)
    else:  # pragma: no cover - covered by the test below as well
        raise AssertionError("a stale config_version must be refused")

    # The newest release's own body, up to the next release heading.
    newest = changelog.partition("\n## [")[2]
    body = newest.partition("\n## [")[0]
    assert "### Upgrading" in body, "the newest release has no upgrading notes"
    notes = body.partition("### Upgrading")[2].partition("\n### ")[0]
    for named in (
        "config_version",
        "data_dir",
        "profile",
        "PONY_ROOT",
        "pony web",
        "pony reindex",
        ",V=",
        "pony reset",
    ):
        assert named in notes, named


def test_the_version_each_of_them_states_is_one_the_loader_accepts() -> None:
    """The point of all three: a user who copies the line gets a config
    that loads."""
    from pony.config import ConfigError, _require_config_version

    _require_config_version({"config_version": CONFIG_VERSION})
    try:
        _require_config_version({"config_version": CONFIG_VERSION - 1})
    except ConfigError:
        pass
    else:  # pragma: no cover - the whole defect was this not raising
        raise AssertionError("a stale config_version must be refused")
