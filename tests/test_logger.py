"""进程独立日志轮转与发行版日志自检回归（无外部请求）。"""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest

from release import test_release


ROOT = Path(__file__).resolve().parents[1]
LOGGER_SOURCE = """
import logging
import os
from pathlib import Path
import sys
import logger
logger.LOG_DIR = Path(sys.argv[1])
logger.LOG_FILE = logger.LOG_DIR / logger.LOG_FILE.name
log = logger.setup_logging()
handler = log.handlers[0]
assert logger.setup_logging() is log
assert log.handlers == [handler]
assert handler.maxBytes == 2_000_000
assert handler.backupCount == 3
assert logger.LOG_FILE.name == f'mcp_search_{os.getpid()}.log'
print('ready', flush=True)
assert sys.stdin.readline().strip() == 'start'
for index in range(20000):
    log.info(
        'record=%s:%s node=tavily ok elapsed=0.1s results=5 '
        "query='ordinary search query about project documentation'",
        os.getpid(), index,
    )
print('done', flush=True)
assert sys.stdin.readline().strip() == 'release'
logging.shutdown()
"""


def test_concurrent_log_rotation(tmp_path: Path) -> None:
    async def exercise():
        processes = []
        try:
            for _ in range(2):
                processes.append(await asyncio.create_subprocess_exec(
                    sys.executable, "-c", LOGGER_SOURCE, str(tmp_path),
                    cwd=ROOT, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                ))
            ready = await asyncio.wait_for(asyncio.gather(*(
                proc.stdout.readline() for proc in processes
            )), 10)
            assert all(line.strip() == b"ready" for line in ready)
            # 两个进程都就绪后同时写入，确保覆盖并发日志轮转。
            for proc in processes:
                proc.stdin.write(b"start\n")
                await proc.stdin.drain()
            done = await asyncio.wait_for(asyncio.gather(*(
                proc.stdout.readline() for proc in processes
            )), 30)
            assert all(line.strip() == b"done" for line in done)
            for proc in processes:
                proc.stdin.write(b"release\n")
                await proc.stdin.drain()
            outputs = await asyncio.wait_for(asyncio.gather(*(
                proc.communicate() for proc in processes
            )), 10)
            assert [proc.returncode for proc in processes] == [0, 0]
            assert all(stderr == b"" for _, stderr in outputs)
            return [proc.pid for proc in processes]
        finally:
            for proc in processes:
                if proc.returncode is None:
                    proc.kill()
            await asyncio.wait_for(asyncio.gather(*(
                proc.communicate() for proc in processes
            )), 10)

    pids = asyncio.run(exercise())
    for pid in pids:
        files = list(tmp_path.glob(f"mcp_search_{pid}.log*"))
        assert (tmp_path / f"mcp_search_{pid}.log.1").is_file()
        assert sum(path.stat().st_size for path in files) > 2_000_000
        records = Counter(
            # 按进程和序号核对全部记录，检测轮转造成的丢失、重复或串写。
            (int(owner), int(index))
            for path in files
            for owner, index in re.findall(
                r"record=(\d+):(\d+)", path.read_text(encoding="utf-8")
            )
        )
        assert records == Counter((pid, index) for index in range(20000))


@pytest.mark.parametrize(
    ("case", "expected"),
    [("pid", 0), ("legacy", 0), ("append", 0), ("stale", 1), ("empty", 1)],
)
def test_release_log_current_run(tmp_path, monkeypatch, case, expected):
    (tmp_path / "search-mcp.exe").touch()
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    legacy = log_dir / "mcp_search.log"
    if case in ("append", "stale"):
        legacy.write_text("old\n", encoding="utf-8")

    @asynccontextmanager
    async def fake_stdio(_params):
        yield None, None

    class FakeSession:
        def __init__(self, _read, _write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def initialize(self):
            return SimpleNamespace(
                server_info=SimpleNamespace(name="Qianxv-search-mcp"),
                protocol_version="2025-11-25",
            )

        async def list_tools(self):
            return SimpleNamespace(tools=[
                SimpleNamespace(name="search"), SimpleNamespace(name="extract"),
            ])

        async def call_tool(self, name, _arguments):
            if name == "search":
                if case in ("pid", "empty"):
                    (log_dir / "mcp_search_123.log").write_text(
                        "call\n" if case == "pid" else "", encoding="utf-8",
                    )
                elif case == "legacy":
                    legacy.write_text("call\n", encoding="utf-8")
                elif case == "append":
                    with legacy.open("a", encoding="utf-8") as stream:
                        stream.write("call\n")
                text = "来源节点: mock\n1. Title"
            else:
                text = "来源节点: mock\n标题: Title\n---\n" + "A" * 60
            return SimpleNamespace(
                is_error=False, content=[SimpleNamespace(text=text)],
            )

    monkeypatch.setattr(test_release, "stdio_client", fake_stdio)
    monkeypatch.setattr(test_release, "ClientSession", FakeSession)
    monkeypatch.setattr(test_release, "_passed", 0)
    monkeypatch.setattr(test_release, "_failed", [])
    assert asyncio.run(test_release.run(tmp_path)) == expected
    assert test_release._failed == (
        ["日志写入 exe 旁 logs/"] if expected else []
    )
