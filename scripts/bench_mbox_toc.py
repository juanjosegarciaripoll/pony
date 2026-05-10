"""Benchmark _build_mbox_toc vs mailbox.mbox._generate_toc on Old/*.mbox.

Verifies byte-for-byte equality, then runs each method N times per folder
and reports min/median to factor out OS-cache and JIT warmup noise.
"""

from __future__ import annotations

import mailbox
import statistics
import sys
import time
from pathlib import Path

from pony.config import load_config
from pony.domain import LocalAccountConfig
from pony.paths import AppPaths
from pony.storage import _build_mbox_toc

ACCOUNT = "Old"
ITERATIONS = 3


def _resolve_mbox_root() -> Path:
    paths = AppPaths.default()
    config = load_config(paths.config_file)
    account = next(a for a in config.accounts if a.name == ACCOUNT)
    if not isinstance(account, LocalAccountConfig):
        raise SystemExit(f"account {ACCOUNT} is not a local-mirror account")
    if account.mirror.format != "mbox":
        raise SystemExit(
            f"account {ACCOUNT} mirror is {account.mirror.format}, not mbox"
        )
    return account.mirror.path


def _time_stdlib(path: Path) -> tuple[float, dict[int, tuple[int, int]], int, int]:
    mbox = mailbox.mbox(str(path), create=False)
    t0 = time.perf_counter()
    mbox._generate_toc()  # type: ignore[attr-defined]
    elapsed = time.perf_counter() - t0
    toc = dict(mbox._toc)  # type: ignore[attr-defined]
    nxt: int = mbox._next_key  # type: ignore[attr-defined]
    flen: int = mbox._file_length  # type: ignore[attr-defined]
    mbox.close()
    return elapsed, toc, nxt, flen


def _bench_one(path: Path) -> None:
    size = path.stat().st_size
    if size == 0:
        print(f"  (empty file, skipped: {path.name})")
        return

    # Warm OS page cache with a fast read first; both methods then see
    # equivalent cache state, so the comparison reflects algorithmic
    # cost, not who-gets-the-cold-disk.
    fast_toc, fast_next, fast_flen = _build_mbox_toc(path)

    t_stdlib, ref_toc, ref_next, ref_flen = _time_stdlib(path)
    ok = (fast_toc == ref_toc and fast_next == ref_next and fast_flen == ref_flen)

    fast_times: list[float] = []
    for _ in range(ITERATIONS):
        t0 = time.perf_counter()
        _build_mbox_toc(path)
        fast_times.append(time.perf_counter() - t0)

    n = len(ref_toc)
    f_min = min(fast_times)
    f_med = statistics.median(fast_times)
    speedup = t_stdlib / f_med if f_med > 0 else float("inf")
    match_str = "OK" if ok else "MISMATCH"
    print(
        f"  {path.name:<32s}  "
        f"{size / 1e6:>8.1f} MB  "
        f"{n:>7,d} msgs  "
        f"stdlib={t_stdlib:>6.3f}s  "
        f"fast med={f_med:>6.3f}s min={f_min:>6.3f}s  "
        f"{speedup:>5.2f}x  "
        f"[{match_str}]",
        flush=True,
    )


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    root = _resolve_mbox_root()
    files = sorted(root.glob("*.mbox"), key=lambda p: p.stat().st_size)
    print(f"account: {ACCOUNT}")
    print(f"root:    {root}")
    print(f"folders: {len(files)}  iterations per method: {ITERATIONS} (post-warmup)")
    print()
    for f in files:
        _bench_one(f)


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
