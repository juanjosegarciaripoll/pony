"""PyInstaller entry point for Pony Express.

Uses an absolute import so it works when executed as a top-level script by
the PyInstaller bootloader (relative imports are not available there).
"""

from pony.cli import main

raise SystemExit(main())
