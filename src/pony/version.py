"""Version information for Pony Express.

The version string below is the single runtime source of truth.  It is
kept in sync with ``pyproject.toml`` by the release workflow, which
updates both files in the same commit.  This avoids any dependency on
:mod:`importlib.metadata`, which is unavailable inside PyInstaller
bundles.
"""

__version__: str = "0.6.0"
