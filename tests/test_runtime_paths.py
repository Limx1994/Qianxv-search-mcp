"""配置、日志、生命周期和启动路径的隔离验证。"""

import argparse
import asyncio
import importlib
import io
import json
import logging
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

import config_loader
import logger as log_module
import providers
import search_router
from config_loader import AppConfig, ConfigError, NodeConfig
from providers.base import SearchResult
from search_router import AllProvidersFailedError, RequestTimeoutError, SearchRouter


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(config_loader, "load_config", lambda: AppConfig(60, []))
    previous = sys.modules.pop("server", None)
    module = importlib.import_module("server")
    yield module
    sys.modules.pop("server", None)
    if previous is not None:
        sys.modules["server"] = previous


@pytest.mark.parametrize("module", [config_loader, log_module])
def test_frozen_app_dir(monkeypatch, tmp_path, module):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "app.exe"))
    assert module._app_dir() == tmp_path


@pytest.mark.parametrize("raw,reason", [
    ("{", "invalid JSON"),
    ({"nodes": [None]}, "must be an object"),
    ({"nodes": [{"type": "tavily"}]}, "empty or duplicate"),
    ({"nodes": [{"name": "a", "type": "tavily"}] * 2}, "empty or duplicate"),
    ({"nodes": [{"name": "a", "type": "tavily", "options": [1]}]}, "options must"),
])
def test_config_error_paths(tmp_path, raw, reason):
    path = tmp_path / "config.json"
    path.write_text(raw if isinstance(raw, str) else json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigError, match=reason):
        config_loader.load_config(path)


@pytest.mark.parametrize("extract", [False, True])
def test_registry_skips_unknown(extract):
    nodes = [NodeConfig("unknown", "unknown", True, "", 1)]
    cfg = AppConfig(60, nodes, nodes)
    build = providers.build_extract_providers if extract else providers.build_providers
    assert build(cfg) == []


def test_logging_fallback(monkeypatch, tmp_path):
    import tempfile
    isolated = logging.Logger("isolated")
    real_get = logging.getLogger
    monkeypatch.setattr(log_module, "logging", SimpleNamespace(
        getLogger=lambda name: isolated if name == "mcp_search" else real_get(name),
        Formatter=logging.Formatter, WARNING=logging.WARNING,
    ))
    blocked = tmp_path / "blocked"
    blocked.write_text("file prevents mkdir", encoding="utf-8")
    monkeypatch.setattr(log_module, "LOG_DIR", blocked)
    monkeypatch.setattr(log_module, "LOG_FILE", blocked / "log")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    try:
        assert log_module.setup_logging() is isolated
        isolated.info("fallback works")
        assert log_module.LOG_DIR == tmp_path / "qianxv-search-mcp-logs"
        assert "fallback works" in log_module.LOG_FILE.read_text(encoding="utf-8")
        assert log_module.setup_logging() is isolated
        assert len(isolated.handlers) == 1
    finally:
        for handler in isolated.handlers:
            handler.close()


def test_lifespan_init_rollback(app, monkeypatch):
    client = httpx.AsyncClient()
    def fail():
        raise RuntimeError("factory failed")
    first = SimpleNamespace(timeout=1, _http_client=None, create_http_client=lambda: client)
    second = SimpleNamespace(timeout=1, _http_client=None, create_http_client=fail)
    monkeypatch.setattr(app._router, "providers", [first, second])
    async def exercise():
        with pytest.raises(RuntimeError, match="factory failed"):
            async with app._lifespan(app.mcp):
                pytest.fail("failed initialization must not yield")
        assert client.is_closed
        assert first._http_client is None and second._http_client is None
        assert app._client_users == 0 and app._client_stack is None
        monkeypatch.setattr(second, "create_http_client", lambda: httpx.AsyncClient())
        monkeypatch.setattr(first, "create_http_client", lambda: httpx.AsyncClient())
        async with app._lifespan(app.mcp):
            assert app._client_users == 1
        assert app._client_users == 0
    asyncio.run(exercise())


def test_lifespan_empty_stack(app, monkeypatch):
    async def exercise():
        async with app._lifespan(app.mcp):
            stack = app._client_stack
            monkeypatch.setattr(app, "_client_stack", None)
            await stack.aclose()
        assert app._client_users == 0
    asyncio.run(exercise())


@pytest.mark.parametrize("value,expected", [("1", 1), ("65535", 65535),
    ("bad", "integer"), ("0", "between"), ("65536", "between")])
def test_port_boundaries(app, value, expected):
    if isinstance(expected, int):
        assert app._port(value) == expected
    else:
        with pytest.raises(argparse.ArgumentTypeError, match=expected):
            app._port(value)


def test_stdio_encoding(app, monkeypatch):
    stream = Mock()
    broken = Mock()
    broken.reconfigure.side_effect = OSError("closed")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", broken)
    app._ensure_utf8_stdio()
    stream.reconfigure.assert_called_once_with(encoding="utf-8")
    broken.reconfigure.assert_called_once_with(encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", object())
    app._ensure_utf8_stdio()


@pytest.mark.parametrize("argv,expected", [
    ([], {"transport": "stdio"}),
    (["--transport", "streamable-http"], {"transport": "streamable-http", "host": "127.0.0.1", "port": 8000}),
    (["--transport", "streamable-http", "--port", "1234"], {"transport": "streamable-http", "host": "127.0.0.1", "port": 1234}),
    (["--transport", "both"], 8000),
    (["--transport", "both", "--port", "1234"], 1234),
    (["--port", "1234"], None),
])
def test_main_dispatch(app, monkeypatch, argv, expected):
    monkeypatch.setattr(sys, "argv", ["server.py", *argv])
    monkeypatch.setattr(app, "_ensure_utf8_stdio", lambda: None)
    run = Mock()
    monkeypatch.setattr(app.mcp, "run", run)
    calls = []
    async def both(port):
        calls.append(port)
    monkeypatch.setattr(app, "_run_both", both)
    if expected is None:
        with pytest.raises(SystemExit) as error:
            app.main()
        assert error.value.code == 2
        run.assert_not_called()
    else:
        app.main()
        if isinstance(expected, dict):
            run.assert_called_once_with(**expected)
        else:
            assert calls == [expected]


def test_script_entry(monkeypatch):
    from mcp.server.mcpserver import MCPServer
    monkeypatch.setattr(config_loader, "load_config", lambda: AppConfig(60, []))
    monkeypatch.setattr(sys, "argv", ["server.py"])
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    run = Mock()
    monkeypatch.setattr(MCPServer, "run", run)
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "server.py"), run_name="__main__")
    run.assert_called_once_with(transport="stdio")


@pytest.mark.parametrize("finish", ["stdio", "http", "stdio_error", "http_error", "cancel"])
def test_both_task_cleanup(app, monkeypatch, finish):
    started = None
    ended = []
    calls = []
    async def transport(kind):
        calls.append(kind)
        if len(calls) == 2:
            started.set()
        try:
            await started.wait()
            if finish == kind:
                if kind == "http":
                    await asyncio.sleep(0.01)
                return
            if finish == kind + "_error":
                raise RuntimeError(kind + " failed")
            if finish == "stdio" and kind == "http":
                await asyncio.sleep(0.01)
                return
            await asyncio.Future()
        finally:
            ended.append(kind)
    monkeypatch.setattr(app.mcp, "streamable_http_app", lambda **kwargs: "app")
    monkeypatch.setattr(app.mcp, "run_stdio_async", lambda: transport("stdio"))
    monkeypatch.setattr(app.uvicorn, "Config", lambda app, **kwargs: kwargs)
    monkeypatch.setattr(app.uvicorn, "Server", lambda cfg: SimpleNamespace(serve=lambda: transport("http")))
    async def exercise():
        nonlocal started
        started = asyncio.Event()
        task = asyncio.create_task(app._run_both(1234))
        if finish == "cancel":
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif finish.endswith("error"):
            with pytest.raises(RuntimeError, match="failed"):
                await task
        else:
            await task
        assert sorted(ended) == ["http", "stdio"]
    asyncio.run(exercise())


@pytest.mark.parametrize("case", ["before", "after", "early_timeout"])
def test_router_deadline_boundaries(monkeypatch, case):
    clock = [0.0]
    monkeypatch.setattr(search_router, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls = []
    async def search(*args):
        calls.append("search")
        if case == "early_timeout":
            raise asyncio.TimeoutError()
        clock[0] = 2
        return [SearchResult("title", "https://example.test", "body", "mock")]
    provider = SimpleNamespace(name="mock", timeout=10, search=search)
    router = SearchRouter([provider], total_timeout=0 if case == "before" else 1)
    error = AllProvidersFailedError if case == "early_timeout" else RequestTimeoutError
    with pytest.raises(error):
        asyncio.run(router.search("q"))
    assert calls == ([] if case == "before" else ["search"])
    assert bool(router._fail_at) is (case == "early_timeout")


def test_empty_error_detail():
    assert str(AllProvidersFailedError([])) == "all providers failed: no enabled node"
