"""Running a block with the process in a named time zone.

`localize_naive`, `is_all_day_span` and the `VALUE=DATE` date ask the
*system* zone, so there is nothing to inject: the environment is the
seam, and a test that does not set it proves whatever the host happens
to be set to -- UTC on CI, where every zone-dependent assertion passes
vacuously.
"""

from __future__ import annotations

import os
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager

#: `time.tzset` is POSIX-only; without it `TZ` does not take effect.
HAVE_TZSET = hasattr(time, "tzset")

requires_tzset = unittest.skipUnless(HAVE_TZSET, "TZ switching needs time.tzset")


@contextmanager
def local_zone(name: str) -> Iterator[None]:
    """Put the process in zone *name* for the block, and restore it after."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = previous
        time.tzset()


__all__ = ["HAVE_TZSET", "local_zone", "requires_tzset"]
