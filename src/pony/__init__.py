"""Pony Express: a mail client, hosting a calendar.

Importing this package imports nothing of the program. The five
components -- the mail model, the calendar model, and the terminal,
browser and command-line front ends over them -- are reached by
importing the module that holds them, so a tool that wants the stores
does not pay for a front end, and nothing that wants a front end
imports another one by accident (`tests/test_layering.py`).

The entry points are `pony.cli:main` (the `pony` command) and
`python -m pony`.
"""

from __future__ import annotations

__all__: list[str] = []
