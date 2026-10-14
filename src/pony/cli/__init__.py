"""The command line, both halves of it.

`program` is the top: it builds the one parser -- `pony --help` lists
mail's commands and `calendar`, `pony calendar --help` lists the
calendar's -- reads the configuration, and dispatches. The commands
themselves are `mail` and `calendar`, and each works through its model
and reports what it did.

The entry points are `pony.cli:main` (the `pony` command) and
`python -m pony`.
"""

from __future__ import annotations

from .program import build_parser, main

__all__ = ["build_parser", "main"]
