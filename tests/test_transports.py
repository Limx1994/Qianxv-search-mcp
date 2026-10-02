"""stdio 和 Streamable HTTP 的无密钥进程级验证。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
from pathlib import Path

import pytest

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


ROOT = Path(__file__).resolve().parents[1]
SOURCE = """
import config_loader
import runpy
from config_loader import AppConfig
config_loader.load_config = lambda: AppConfig(60, [])
runpy.run_module('server', run_name='__main__')
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _start(
    *args: str, executable: Path | None = None
) -> asyncio.subprocess.Process:
    command = (
        [sys.executable, "-c", SOURCE]
        if executable is None else [str(executable)]
    )
    return await asyncio.create_subprocess_exec(
        *command, *args,
        cwd=ROOT if executable is None else executable.parent,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _stop(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(proc.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            proc.terminate()
    await asyncio.wait_for(proc.communicate(), 20)


async def _rpc(proc: asyncio.subprocess.Process, message: dict) -> dict:
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write((json.dumps(message) + "\n").encode())
    await proc.stdin.drain()
    while True:
        line = await asyncio.wait_for(proc.stdout.readline(), 15)
        assert line, "stdio closed before response"
        response = json.loads(line)
        if response.get("id") == message["id"]:
            return response


async def _check_stdio(proc: asyncio.subprocess.Process) -> None:
    init = await _rpc(proc, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    })
    assert init["result"]["serverInfo"]["name"] == "Qianxv-search-mcp"
    proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    await proc.stdin.drain()
    listed = await _rpc(proc, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    })
    assert {item["name"] for item in listed["result"]["tools"]} == {
        "search", "extract",
    }
    called = await _rpc(proc, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "search", "arguments": {"query": "test"}},
    })
    assert "所有节点均不可用" in called["result"]["content"][0]["text"]


async def _check_http(port: int) -> None:
    url = f"http://127.0.0.1:{port}/mcp"
    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.server_info.name == "Qianxv-search-mcp"
            listed = await session.list_tools()
            assert {tool.name for tool in listed.tools} == {"search", "extract"}
            called = await session.call_tool("extract", {"url": "https://example.test"})
            assert "所有节点均不可用" in called.content[0].text


async def _wait_http(port: int) -> None:
    for _ in range(300):
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise AssertionError("HTTP server did not start")


def test_stdio_default() -> None:
    async def exercise() -> None:
        proc = await _start()
        try:
            await _check_stdio(proc)
            proc.stdin.close()
            assert await asyncio.wait_for(proc.wait(), 10) == 0
        finally:
            await _stop(proc)

    asyncio.run(exercise())


def test_http_only() -> None:
    async def exercise() -> None:
        port = _free_port()
        proc = await _start("--transport", "streamable-http", "--port", str(port))
        try:
            await _wait_http(port)
            await _check_http(port)
        finally:
            await _stop(proc)

    asyncio.run(exercise())


def test_both_survives_stdio_disconnect() -> None:
    async def exercise() -> None:
        port = _free_port()
        proc = await _start("--transport", "both", "--port", str(port))
        try:
            await _wait_http(port)
            await _check_stdio(proc)
            await _check_http(port)
            proc.stdin.close()
            await asyncio.sleep(0.1)
            assert proc.returncode is None
            await _check_http(port)
        finally:
            await _stop(proc)

    asyncio.run(exercise())


@pytest.mark.parametrize("transport", ["streamable-http", "both"])
def test_http_port_conflict(transport: str) -> None:
    async def exercise() -> None:
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            proc = await _start("--transport", transport, "--port", str(port))
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), 15)
                assert proc.returncode != 0
                assert b"error" in stderr.lower(), stderr.decode(errors="replace")
            finally:
                if proc.returncode is None:
                    await _stop(proc)

    asyncio.run(exercise())


@pytest.fixture
def release_exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    value = os.getenv("MCP_TEST_EXE")
    if not value:
        pytest.skip("exe path not set")
    source = Path(value).resolve()
    assert source.is_file()
    app = tmp_path / "app"
    app.mkdir()
    exe = app / source.name
    shutil.copy2(source, exe)
    internal = source.parent / "_internal"
    if internal.is_dir():
        shutil.copytree(internal, app / "_internal")
    config = json.loads(
        (source.parent / "config.example.json").read_text(
            encoding="utf-8"
        )
    )
    for node in (*config["nodes"], *config.get("extract_nodes", [])):
        node["enabled"] = False
        node["api_key"] = ""
    (app / "config.json").write_text(json.dumps(config), encoding="utf-8")
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setenv("TEMP", str(temp))
    monkeypatch.setenv("TMP", str(temp))
    return exe


def _check_no_mei() -> None:
    assert not list(Path(os.environ["TEMP"]).glob("_MEI*")), (
        "release exe extracted dependencies into TEMP"
    )


def test_release_exe_modes(release_exe: Path) -> None:
    exe = release_exe

    async def exercise() -> None:
        proc = await _start(executable=exe)
        try:
            await _check_stdio(proc)
            proc.stdin.close()
            assert await asyncio.wait_for(proc.wait(), 15) == 0
        finally:
            await _stop(proc)

        port = _free_port()
        proc = await _start(
            "--transport", "streamable-http", "--port", str(port),
            executable=exe,
        )
        try:
            await _wait_http(port)
            await _check_http(port)
        finally:
            await _stop(proc)

        port = _free_port()
        proc = await _start(
            "--transport", "both", "--port", str(port), executable=exe,
        )
        try:
            await _wait_http(port)
            await _check_stdio(proc)
            await _check_http(port)
            proc.stdin.close()
            await asyncio.sleep(0.1)
            assert proc.returncode is None
            await _check_http(port)
        finally:
            await _stop(proc)

        for transport in ("streamable-http", "both"):
            with socket.socket() as occupied:
                occupied.bind(("127.0.0.1", 0))
                occupied.listen()
                proc = await _start(
                    "--transport", transport, "--port",
                    str(occupied.getsockname()[1]), executable=exe,
                )
                try:
                    _, stderr = await asyncio.wait_for(proc.communicate(), 15)
                    assert proc.returncode != 0
                    assert b"error" in stderr.lower()
                finally:
                    await _stop(proc)

    asyncio.run(exercise())


@pytest.mark.parametrize("transport", ["stdio", "streamable-http", "both"])
def test_release_no_temp_repeated(release_exe: Path, transport: str) -> None:
    async def exercise() -> None:
        for _ in range(5):
            port = _free_port()
            args = () if transport == "stdio" else (
                "--transport", transport, "--port", str(port),
            )
            proc = await _start(*args, executable=release_exe)
            try:
                if transport in ("stdio", "both"):
                    await _check_stdio(proc)
                if transport != "stdio":
                    await _wait_http(port)
                    await _check_http(port)
                _check_no_mei()
                if transport == "stdio":
                    proc.stdin.close()
                    assert await asyncio.wait_for(proc.wait(), 15) == 0
            finally:
                await _stop(proc)
            _check_no_mei()

    asyncio.run(exercise())


def test_release_no_temp_concurrent(release_exe: Path) -> None:
    async def exercise() -> None:
        processes = []
        try:
            for _ in range(3):
                processes.append(await _start(executable=release_exe))
            await asyncio.gather(*(_check_stdio(proc) for proc in processes))
            _check_no_mei()
            for proc in processes:
                proc.stdin.close()
            codes = await asyncio.wait_for(
                asyncio.gather(*(proc.wait() for proc in processes)), 15
            )
            assert codes == [0, 0, 0]
        finally:
            await asyncio.gather(*(_stop(proc) for proc in processes))
        _check_no_mei()

    asyncio.run(exercise())
