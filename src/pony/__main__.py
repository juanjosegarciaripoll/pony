"""Module entrypoint for ``python -m pony``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
