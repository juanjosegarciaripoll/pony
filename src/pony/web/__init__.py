"""The browser front end.

Importing this package pulls in Starlette, which is an optional
dependency (``pip install pony[web]``); nothing outside it imports it at
module level, so a terminal-only installation never touches it.
"""

from __future__ import annotations

from .app import WebServices, build_web_app

__all__ = ["WebServices", "build_web_app"]
