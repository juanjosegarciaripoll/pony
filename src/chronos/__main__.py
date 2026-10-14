"""``python -m chronos``, which the calendar cannot answer on its own.

The calendar is hosted: its root is handed to it by the application
that embeds it, and it resolves no platform directory of its own (see
:mod:`chronos.paths`).  So there is nowhere for an unhosted run to put
its index, mirror and tokens, and this said so by raising
``RuntimeError: calendar paths are not set`` out of the first command
that touched a file.  It says so in a sentence instead.
"""

import sys

if __name__ == "__main__":
    sys.stderr.write(
        "The calendar runs inside Pony Express, which tells it where its "
        "files live.\nRun `pony calendar ...` -- `pony calendar --help` "
        "lists the commands.\n"
    )
    sys.exit(2)
