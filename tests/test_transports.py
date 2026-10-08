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
# 子进程启动前替换配置加载，避免读取私有配置或调用外部服务。
SOURCE = """
import config_loader
import runpy
from config_loader import AppConfig
config_loader.load_config = lambda: AppConfig(60, [])
runpy.run_module('server', run_name='__main__')
"""

LONG_SOURCE = """
import config_loader
import providers
import runpy
from config_loader import AppConfig
from providers.extract_base import ExtractResult
config_loader.load_config = lambda: AppConfig(60, [])
class FakeExtract:
    name = 'mock'
    timeout = 1.0
    _http_client = None
    async def extract(self, url):
        return ExtractResult('Page', url, '文' * 8000 + 'TARGET', 'mock')
providers.build_extract_providers = lambda cfg: [FakeExtract()]
runpy.run_module('server', run_name='__main__')
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _start(
    *args: str, executable: Path | None = None, source: str = SOURCE
) -> asyncio.subprocess.Process:
    command = (
        [sys.executable, "-c", source]
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
            # Windows 下同时终止进程树，避免发行版启动器留下子进程。
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
        # stdio 可能穿插通知，只接收与本次请求 id 对应的响应。
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
    assert called["result"]["isError"] is True


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
            assert called.is_error is True


def _page_cursor(output: str) -> str:
    assert output.split("---\n", 1)[1] == "文" * 8000
    assert "has_more: true" in output
    return output.split("snapshot_id: ", 1)[1].splitlines()[0]


async def _check_long_stdio(proc: asyncio.subprocess.Process) -> None:
    await _rpc(proc, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    })
    assert proc.stdin is not None
    proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    await proc.stdin.drain()
    url = "https://example.test/long"
    first = await _rpc(proc, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "extract", "arguments": {"url": url}},
    })
    snapshot_id = _page_cursor(first["result"]["content"][0]["text"])
    second = await _rpc(proc, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "extract", "arguments": {
            "url": url, "offset": 8000, "snapshot_id": snapshot_id,
        }},
    })
    assert second["result"]["content"][0]["text"].endswith("---\nTARGET")


async def _check_long_http(port: int) -> None:
    url = "https://example.test/long"
    # 首次抓取与续读使用不同 HTTP 会话，验证快照由服务持有而非绑定客户端。
    async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (
        read, write,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            first = await session.call_tool("extract", {"url": url})
            snapshot_id = _page_cursor(first.content[0].text)
    async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (
        read, write,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            second = await session.call_tool("extract", {
                "url": url, "offset": 8000, "snapshot_id": snapshot_id,
            })
            assert second.content[0].text.endswith("---\nTARGET")


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


def test_release_socks_extract(release_exe: Path, monkeypatch, tmp_path: Path) -> None:
    import ssl

    from test_local_network import certificate

    declared = json.loads((release_exe.parent / "config.json").read_text(encoding="utf-8"))
    if not any(node["type"] == "local_extract" for node in declared.get("extract_nodes", [])):
        pytest.skip("release template does not declare local node support")
    cert_path, key_path = certificate(tmp_path, "example.test", "93.184.216.34")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_path))
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")

    async def exercise():
        body = ("<html><head><title>SOCKS article</title></head><body><article><p>"
                + "The packaged SOCKS transport extracts this article correctly. " * 20
                + "</p></article></body></html>").encode()
        requests = []

        async def upstream(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
            writer.close()

        async def relay(reader, writer):
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()

        async with await asyncio.start_server(upstream, "127.0.0.1", 0, ssl=context) as tls_server:
            tls_port = tls_server.sockets[0].getsockname()[1]

            async def socks(reader, writer):
                version, methods = await reader.readexactly(2)
                assert version == 5
                await reader.readexactly(methods)
                writer.write(b"\x05\x00")
                await writer.drain()
                assert await reader.readexactly(4) == b"\x05\x01\x00\x01"
                address = socket.inet_ntoa(await reader.readexactly(4))
                port = int.from_bytes(await reader.readexactly(2), "big")
                requests.append((address, port))
                peer_read, peer_write = await asyncio.open_connection("127.0.0.1", tls_port)
                writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
                await writer.drain()
                await asyncio.gather(relay(reader, peer_write), relay(peer_read, writer))

            async with await asyncio.start_server(socks, "127.0.0.1", 0) as proxy:
                proxy_port = proxy.sockets[0].getsockname()[1]
                monkeypatch.setenv("HTTPS_PROXY", f"socks5://127.0.0.1:{proxy_port}")
                config = {
                    "nodes": [{"name": "local-search", "type": "local_search"}],
                    "extract_nodes": [{"name": "local-extract", "type": "local_extract"}],
                }
                (release_exe.parent / "config.json").write_text(json.dumps(config), encoding="utf-8")
                proc = await _start(executable=release_exe)
                try:
                    await _rpc(proc, {
                        "jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                   "clientInfo": {"name": "test", "version": "1"}},
                    })
                    proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                    await proc.stdin.drain()
                    response = await _rpc(proc, {
                        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "extract", "arguments": {"url": "https://93.184.216.34/article"}},
                    })
                    result = response["result"]
                    assert not result.get("isError", False), result
                    assert "SOCKS article" in result["content"][0]["text"]
                    proc.stdin.close()
                    assert await asyncio.wait_for(proc.wait(), 15) == 0
                finally:
                    await _stop(proc)
        assert requests == [("93.184.216.34", 443)]
        _check_no_mei()

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


@pytest.mark.parametrize("transport", ["stdio", "streamable-http", "both"])
def test_source_long_extract_paging(transport: str) -> None:
    async def exercise() -> None:
        port = _free_port()
        args = () if transport == "stdio" else (
            "--transport", transport, "--port", str(port),
        )
        proc = await _start(*args, source=LONG_SOURCE)
        try:
            if transport != "stdio":
                await _wait_http(port)
                await _check_long_http(port)
            if transport != "streamable-http":
                await _check_long_stdio(proc)
            if transport == "stdio":
                assert proc.stdin is not None
                proc.stdin.close()
                assert await asyncio.wait_for(proc.wait(), 10) == 0
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


@pytest.fixture(scope="session")
def release_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    value = os.getenv("MCP_TEST_EXE")
    if not value:
        # 没有待测发行版时明确跳过，源码测试通过不能代替 exe 验证。
        pytest.skip("exe path not set")
    source = Path(value).resolve()
    assert source.is_file()
    app = tmp_path_factory.mktemp("release_bin")
    exe = app / source.name
    shutil.copy2(source, exe)
    internal = source.parent / "_internal"
    if internal.is_dir():
        shutil.copytree(internal, app / "_internal")
    shutil.copy2(source.parent / "config.example.json", app)
    return exe


@pytest.fixture
def release_exe(
    release_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    app = tmp_path / "app"
    app.mkdir()
    exe = app / release_bin.name
    os.link(release_bin, exe)
    internal = release_bin.parent / "_internal"
    if internal.is_dir():
        shutil.copytree(internal, app / "_internal", copy_function=os.link)
    config = json.loads(
        (release_bin.parent / "config.example.json").read_text(
            encoding="utf-8"
        )
    )
    for node in (*config["nodes"], *config.get("extract_nodes", [])):
        # 使用无密钥、全禁用节点的隔离配置，仅验证传输和进程行为。
        node["enabled"] = False
        node["api_key"] = ""
    (app / "config.json").write_text(json.dumps(config), encoding="utf-8")
    temp = tmp_path / "temp"
    temp.mkdir()
    # 将临时解包目录隔离到本用例中，便于检查 _MEI 残留。
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


@pytest.mark.parametrize("transport", ["stdio", "streamable-http", "both"])
def test_release_long_extract_paging(release_exe: Path, transport: str) -> None:
    async def exercise() -> None:
        requests = []

        async def respond(reader, writer):
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(line.split(b":", 1)[1]) for line in header.splitlines()
                if line.lower().startswith(b"content-length:")
            )
            requests.append(json.loads(await reader.readexactly(length)))
            body = json.dumps({"data": {
                "url": "https://example.test/long", "title": "Page",
                "content": "文" * 8000 + "TARGET",
            }}, ensure_ascii=False).encode("utf-8")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        async with await asyncio.start_server(respond, "127.0.0.1", 0) as upstream:
            upstream_port = upstream.sockets[0].getsockname()[1]
            path = release_exe.parent / "config.json"
            config = json.loads(path.read_text(encoding="utf-8"))
            config["extract_nodes"] = [{
                "name": "mock", "type": "anysearch_extract", "enabled": True,
                "api_key": "mock-key", "timeout_seconds": 5,
                "options": {"endpoint": f"http://127.0.0.1:{upstream_port}/extract"},
            }]
            path.write_text(json.dumps(config), encoding="utf-8")
            port = _free_port()
            args = () if transport == "stdio" else (
                "--transport", transport, "--port", str(port),
            )
            proc = await _start(*args, executable=release_exe)
            try:
                if transport != "stdio":
                    await _wait_http(port)
                    await _check_long_http(port)
                if transport != "streamable-http":
                    await _check_long_stdio(proc)
                if transport == "stdio":
                    proc.stdin.close()
                    assert await asyncio.wait_for(proc.wait(), 15) == 0
                _check_no_mei()
            finally:
                await _stop(proc)
            _check_no_mei()
            expected = 2 if transport == "both" else 1
            assert requests == [{"url": "https://example.test/long"}] * expected

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["search", "extract"])
@pytest.mark.parametrize("budget", [None, 0.15, 0.05], ids=["default", "equal", "shorter"])
def test_release_timeout_breaker(release_exe: Path, kind: str, budget) -> None:
    async def exercise() -> None:
        requests = []

        async def stalled(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                requests.append(1)
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_server(stalled, "127.0.0.1", 0) as upstream:
            port = upstream.sockets[0].getsockname()[1]
            config = {"nodes": [{"name": "disabled", "type": "tavily", "enabled": False}]}
            section = "nodes" if kind == "search" else "extract_nodes"
            config[section] = [{
                "name": "mock", "type": "tavily" if kind == "search" else "anysearch_extract",
                "enabled": True, "api_key": "mock-key", "timeout_seconds": 0.15,
                "options": {"endpoint": f"http://127.0.0.1:{port}/{kind}"},
            }]
            if budget is not None:
                config["failover"] = {f"{kind}_timeout_seconds": budget}
            (release_exe.parent / "config.json").write_text(json.dumps(config), encoding="utf-8")
            proc = await _start(executable=release_exe)
            try:
                await _rpc(proc, {
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                })
                proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                await proc.stdin.drain()
                arguments = {"query": "test"} if kind == "search" else {"url": "https://example.test/a"}
                for request_id in (2, 3):
                    response = await _rpc(proc, {
                        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                        "params": {"name": kind, "arguments": arguments},
                    })
                    assert response["result"]["isError"] is True
                    text = response["result"]["content"][0]["text"]
                    if request_id == 2 and budget != 0.05:
                        assert "node timeout" in text
                    if request_id == 3 and budget != 0.05:
                        assert "breaker open" in text
                proc.stdin.close()
                assert await asyncio.wait_for(proc.wait(), 15) == 0
            finally:
                await _stop(proc)
            assert len(requests) == (2 if budget == 0.05 else 1)

    asyncio.run(exercise())


@pytest.mark.parametrize("transport", ["stdio", "streamable-http", "both"])
def test_release_local_extract(release_exe: Path, monkeypatch, transport: str) -> None:
    declared = json.loads((release_exe.parent / "config.json").read_text(encoding="utf-8"))
    if not any(node["type"] == "local_extract" for node in declared.get("extract_nodes", [])):
        pytest.skip("release template does not declare local node support")

    async def exercise() -> None:
        requests = []
        body = ("<html><head><meta charset='utf-8'><title>本机长文章</title></head><body><article>"
                + "".join(f"<p>段落 {i}。" + "这是需要完整分页的正文内容。" * 30 + "</p>" for i in range(25))
                + "</article></body></html>").encode("utf-8")

        async def respond(reader, writer):
            try:
                header = await reader.readuntil(b"\r\n\r\n")
                requests.append(header)
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                    + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_server(respond, "127.0.0.1", 0) as proxy:
            proxy_port = proxy.sockets[0].getsockname()[1]
            monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy_port}")
            monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
            # 使用公开 IP 和本机测试代理，无需公网 DNS 或外部服务。
            config = {
                "nodes": [{"name": "local-search", "type": "local_search",
                           "options": {"backend": "bing"}}],
                "extract_nodes": [{"name": "local-extract", "type": "local_extract"}],
            }
            (release_exe.parent / "config.json").write_text(json.dumps(config), encoding="utf-8")
            port = _free_port()
            args = () if transport == "stdio" else (
                "--transport", transport, "--port", str(port),
            )
            proc = await _start(*args, executable=release_exe)
            checked_search = False

            async def check(call):
                nonlocal checked_search
                if not checked_search:
                    error, text = await call("search", {"query": "中文"})
                    assert error and "unsupported or disabled text backend" in text
                    checked_search = True
                url = "http://93.184.216.34/article"
                arguments = {"url": url}
                parts = []
                while True:
                    error, text = await call("extract", arguments)
                    assert not error, text
                    headers, content = text.split("---\n", 1)
                    assert "来源节点: local-extract" in headers
                    assert "标题: 本机长文章" in headers
                    parts.append(content)
                    fields = dict(line.split(": ", 1) for line in headers.splitlines() if ": " in line)
                    if fields["has_more"] == "false":
                        break
                    arguments = {"url": url, "offset": int(fields["next_offset"]),
                                 "snapshot_id": fields["snapshot_id"]}
                joined = "".join(parts)
                assert len(joined) == int(fields["total_chars"]) > 8000
                assert "段落 0。" in joined and "段落 24。" in joined

            try:
                if transport != "stdio":
                    await _wait_http(port)
                    async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()

                            async def call_http(name, arguments):
                                result = await session.call_tool(name, arguments)
                                return result.is_error, result.content[0].text

                            await check(call_http)
                if transport != "streamable-http":
                    await _rpc(proc, {
                        "jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                   "clientInfo": {"name": "test", "version": "1"}},
                    })
                    proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                    await proc.stdin.drain()
                    request_id = 1

                    async def call_stdio(name, arguments):
                        nonlocal request_id
                        request_id += 1
                        response = await _rpc(proc, {
                            "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                            "params": {"name": name, "arguments": arguments},
                        })
                        result = response["result"]
                        return result.get("isError", False), result["content"][0]["text"]

                    await check(call_stdio)
                if transport == "stdio":
                    proc.stdin.close()
                    assert await asyncio.wait_for(proc.wait(), 15) == 0
            finally:
                await _stop(proc)
            _check_no_mei()
            assert len(requests) == (2 if transport == "both" else 1)
            assert all(b"GET http://93.184.216.34/article " in req for req in requests)
            assert all(b"authorization:" not in req.lower() for req in requests)

    asyncio.run(exercise())
