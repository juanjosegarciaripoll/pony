"""Subprocess-isolated checks for Textual's real DirectoryTree lifecycle."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_SCENARIO = textwrap.dedent(
    r"""
    import asyncio
    import sys
    import tempfile
    from pathlib import Path

    from textual.app import App, ComposeResult
    from textual.widgets import DirectoryTree


    def mark(phase: str) -> None:
        print(phase, file=sys.__stdout__, flush=True)


    async def wait_until(predicate, *, timeout: float = 5.0) -> None:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.01)


    async def keep_headless_loop_awake() -> None:
        # DirectoryTree completes filesystem reads on a thread. A real terminal
        # continuously wakes Textual's loop; this otherwise-idle subprocess does
        # not always wake when that thread completes.
        while True:
            await asyncio.sleep(0.01)


    class TreeApp(App[None]):
        def __init__(self, root: Path) -> None:
            super().__init__()
            self.root_path = root

        def compose(self) -> ComposeResult:
            yield DirectoryTree(self.root_path, id="tree")


    async def main() -> None:
        scenario = sys.argv[1]
        root = Path(tempfile.mkdtemp(prefix="pony-real-tree-"))
        nested = root / "nested"
        nested.mkdir()
        (root / "message.txt").write_text("root", encoding="utf-8")
        (nested / "inside.txt").write_text("nested", encoding="utf-8")

        mark("PHASE_BEFORE_APP")
        app = TreeApp(root)
        heartbeat = asyncio.create_task(keep_headless_loop_awake())
        try:
            async with app.run_test() as pilot:
                mark("PHASE_MOUNTED")
                tree = app.query_one(DirectoryTree)
                await wait_until(lambda: len(tree.root.children) == 2)
                mark("PHASE_ROOT_LOADED")

                names = {
                    node.data.path.name for node in tree.root.children if node.data
                }
                assert names == {"message.txt", "nested"}

                if scenario == "expand":
                    nested_node = next(
                        node
                        for node in tree.root.children
                        if node.data and node.data.path == nested
                    )
                    nested_node.expand()
                    await wait_until(lambda: len(nested_node.children) == 1)
                    assert [
                        node.data.path.name
                        for node in nested_node.children
                        if node.data
                    ] == ["inside.txt"]
                elif scenario == "path":
                    other = root / "other"
                    other.mkdir()
                    (other / "second.txt").write_text("other", encoding="utf-8")
                    tree.path = other
                    await wait_until(
                        lambda: tree.root.data is not None
                        and tree.root.data.path == other
                        and len(tree.root.children) == 1
                    )
                    assert tree.root.data is not None
                    assert tree.root.data.path == other
                    assert [
                        node.data.path.name
                        for node in tree.root.children
                        if node.data
                    ] == ["second.txt"]
                else:
                    raise AssertionError(f"unknown scenario: {scenario}")

                mark("PHASE_OPERATION_OK")
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

        loop = asyncio.get_running_loop()
        executor = loop._default_executor
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        mark("DIRECTORY_TREE_SHUTDOWN_OK")


    asyncio.run(main())
    """
)


@pytest.mark.parametrize("scenario", ["expand", "path"])
def test_real_directory_tree_contract_and_shutdown(
    scenario: str,
    tmp_path: Path,
) -> None:
    """Exercise the production widget without risking the pytest process."""
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _SCENARIO, scenario],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(
            f"real DirectoryTree scenario {scenario!r} timed out; "
            f"stdout={error.stdout!r}; stderr={error.stderr!r}"
        )

    assert completed.returncode == 0, completed.stderr
    assert "DIRECTORY_TREE_SHUTDOWN_OK" in completed.stdout
