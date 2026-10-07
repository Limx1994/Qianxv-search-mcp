"""v2.8 超时、熔断恢复、错误返回及配置校验回归（无外部请求）。"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
import time

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import config_loader
from config_loader import AppConfig, ConfigError, load_config
from providers.base import ProviderError, SearchResult
from providers.extract_base import ExtractResult
from search_router import (
    AllProvidersFailedError,
    ExtractRouter,
    RequestTimeoutError,
    SearchRouter,
)


class FakeNode:
    def __init__(self, name, *, empty=False, error=False, blocked=False):
        self.name = name
        self.timeout = 1.0
        self.empty = empty
        self.error = error
        self.blocked = blocked
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def _call(self):
        self.calls += 1
        self.started.set()
        try:
            if self.blocked:
                await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error:
            raise ProviderError(self.name, "mock failure")

    async def search(self, query, max_results):
        await self._call()
        if self.empty:
            return []
        return [SearchResult("Title", "https://example.test/a", "Text", self.name)]

    async def extract(self, url):
        await self._call()
        return ExtractResult("Title", url, "Text", self.name)


def _router(kind, nodes, **kwargs):
    cls = SearchRouter if kind == "search" else ExtractRouter
    return cls(nodes, **kwargs)


async def _call(router, kind):
    return await getattr(router, kind)("https://example.test/a")


@pytest.mark.parametrize("kind", ["search", "extract"])
def test_node_timeout_and_failover(kind, caplog):
    async def exercise():
        slow, good = FakeNode("slow", blocked=True), FakeNode("good")
        slow.timeout = 0.03
        router = _router(kind, [slow, good])
        assert router.total_timeout == 1.03
        _, hit = await asyncio.wait_for(_call(router, kind), 2)
        assert hit == "good"
        assert slow.cancelled and "slow" in router._fail_at
        assert not router._probing

    with caplog.at_level(logging.INFO, logger="mcp_search"):
        logger = logging.getLogger("mcp_search")
        logger.addHandler(caplog.handler)
        try:
            asyncio.run(exercise())
        finally:
            logger.removeHandler(caplog.handler)
    assert "failed elapsed=" in caplog.text
    assert "attempted=2 skipped=0" in caplog.text


@pytest.mark.parametrize("kind", ["search", "extract"])
def test_total_budget_does_not_break_node(kind):
    async def exercise():
        slow, good = FakeNode("slow", blocked=True), FakeNode("good")
        router = _router(kind, [slow, good], total_timeout=0.03)
        with pytest.raises(RequestTimeoutError, match="total budget exhausted"):
            await asyncio.wait_for(_call(router, kind), 2)
        assert slow.cancelled and good.calls == 0
        assert router._fail_at == {} and not router._probing

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["search", "extract"])
@pytest.mark.parametrize("budget", [None, 0.03, 0.01, 0.1], ids=["default", "equal", "shorter", "longer"])
def test_single_node_timeout_breaker(kind, budget):
    async def exercise():
        node = FakeNode("node", blocked=True)
        node.timeout = 0.03
        router = _router(kind, [node], total_timeout=budget)
        for _ in range(2):
            with pytest.raises(AllProvidersFailedError):
                await asyncio.wait_for(_call(router, kind), 2)
        if budget == 0.01:
            assert node.calls == 2 and router._fail_at == {}
        else:
            assert node.calls == 1 and "node" in router._fail_at
        assert node.cancelled and not router._probing

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["search", "extract"])
def test_default_probe_timeout_reopens(kind):
    async def exercise():
        node = FakeNode("node", blocked=True)
        node.timeout = 0.03
        router = _router(kind, [node])
        before = time.monotonic() - 61
        router._fail_at[node.name] = before
        with pytest.raises(RequestTimeoutError):
            await _call(router, kind)
        assert router._fail_at[node.name] > before
        assert not router._probing
        with pytest.raises(AllProvidersFailedError):
            await _call(router, kind)
        assert node.calls == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["search", "extract"])
def test_cleanup_delay_keeps_node_failure(kind):
    async def exercise():
        node, backup = FakeNode("node"), FakeNode("backup")
        node.timeout = 0.03

        async def slow_cleanup():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.06)
                raise

        node._call = slow_cleanup
        router = _router(kind, [node, backup], total_timeout=0.05)
        with pytest.raises(RequestTimeoutError):
            await _call(router, kind)
        assert "node" in router._fail_at and "backup" not in router._fail_at
        assert backup.calls == 0 and not router._probing

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["search", "extract"])
def test_budget_is_shared_between_nodes(kind):
    async def exercise():
        first, second, last = (
            FakeNode("first", blocked=True), FakeNode("second", blocked=True),
            FakeNode("last"),
        )
        first.timeout = 0.03
        router = _router(kind, [first, second, last], total_timeout=0.08)
        with pytest.raises(RequestTimeoutError):
            await asyncio.wait_for(_call(router, kind), 2)
        assert first.cancelled and second.cancelled
        assert first.calls == second.calls == 1 and last.calls == 0
        assert set(router._fail_at) == {"first"}

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["search", "extract"])
@pytest.mark.parametrize("probe", [False, True])
def test_cancellation_releases_probe(kind, probe):
    async def exercise():
        node = FakeNode("node", blocked=True)
        router = _router(kind, [node])
        if probe:
            router._fail_at[node.name] = time.monotonic() - 61
        before = dict(router._fail_at)
        task = asyncio.create_task(_call(router, kind))
        await asyncio.wait_for(node.started.wait(), 2)
        assert bool(router._probing) == probe
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert node.cancelled and not router._probing
        assert router._fail_at == before
        node.blocked = False
        assert (await _call(router, kind))[1] == "node"
        assert router._fail_at == {}

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["search", "extract"])
@pytest.mark.parametrize("error", [False, True])
def test_recovery_only_allows_one_probe(kind, error):
    async def exercise():
        node = FakeNode("node", blocked=True, error=error)
        backup = FakeNode("backup")
        router = _router(kind, [node, backup])
        router._fail_at[node.name] = time.monotonic() - 61
        task = asyncio.create_task(_call(router, kind))
        await asyncio.wait_for(node.started.wait(), 2)
        results = await asyncio.gather(*(_call(router, kind) for _ in range(12)))
        assert all(hit == "backup" for _, hit in results)
        assert node.calls == 1
        node.release.set()
        assert (await task)[1] == ("backup" if error else "node")
        assert not router._probing
        assert (node.name in router._fail_at) == error

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["search", "extract"])
def test_probe_budget_releases_ownership(kind):
    async def exercise():
        node = FakeNode("node", blocked=True)
        router = _router(kind, [node], total_timeout=0.03)
        router._fail_at[node.name] = time.monotonic() - 61
        before = dict(router._fail_at)
        with pytest.raises(RequestTimeoutError):
            await _call(router, kind)
        assert not router._probing and router._fail_at == before
        node.blocked = False
        assert (await _call(router, kind))[1] == "node"

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["search", "extract"])
def test_zero_breaker_disables_cooldown(kind):
    async def exercise():
        node, backup = FakeNode("node", error=True), FakeNode("backup")
        router = _router(kind, [node, backup], breaker_seconds=0)
        assert (await _call(router, kind))[1] == "backup"
        assert (await _call(router, kind))[1] == "backup"
        assert node.calls == 2 and not router._probing

    asyncio.run(exercise())


def test_empty_result_keeps_node_available():
    async def exercise():
        node, backup = FakeNode("node", empty=True), FakeNode("backup")
        router = SearchRouter([node, backup])
        assert (await router.search("unmatched"))[1] == "backup"
        assert router._fail_at == {}
        node.empty = False
        assert (await router.search("normal"))[1] == "node"
        assert node.calls == 2

    asyncio.run(exercise())


@pytest.mark.parametrize("error", [False, True])
def test_empty_result_is_normal_with_errors(error):
    async def exercise():
        router = SearchRouter([FakeNode("empty", empty=True), FakeNode("next", empty=True, error=error)])
        results, hit = await router.search("unmatched")
        assert results == [] and hit == ("empty" if error else "next")
        assert "empty" not in router._fail_at

    asyncio.run(exercise())


def test_empty_probe_closes_breaker():
    async def exercise():
        node = FakeNode("node", empty=True)
        router = SearchRouter([node])
        router._fail_at[node.name] = time.monotonic() - 61
        assert await router.search("unmatched") == ([], "node")
        assert router._fail_at == {} and not router._probing

    asyncio.run(exercise())


def _config_path(tmp_path, raw):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


@pytest.mark.parametrize("value", [0, -1, "NaN", "Infinity", True, None, "bad"])
@pytest.mark.parametrize("field", ["timeout_seconds", "search_timeout_seconds", "extract_timeout_seconds"])
def test_config_rejects_invalid_timeout(tmp_path, field, value):
    node = {"name": "test", "type": "tavily"}
    raw = {"nodes": [node], "failover": {}}
    target = node if field == "timeout_seconds" else raw["failover"]
    target[field] = value
    with pytest.raises(ConfigError, match=field):
        load_config(_config_path(tmp_path, raw))


@pytest.mark.parametrize("value", [-1, "NaN", "Infinity", True, None, "bad"])
def test_config_rejects_invalid_breaker(tmp_path, value):
    raw = {"nodes": [{"name": "test", "type": "tavily"}], "failover": {"breaker_seconds": value}}
    with pytest.raises(ConfigError, match="breaker_seconds"):
        load_config(_config_path(tmp_path, raw))


@pytest.mark.parametrize("value", ["false", 0, 1, None, []])
def test_config_requires_boolean_enabled(tmp_path, value):
    raw = {"nodes": [{"name": "test", "type": "tavily", "enabled": value}]}
    with pytest.raises(ConfigError, match="enabled"):
        load_config(_config_path(tmp_path, raw))


@pytest.mark.parametrize("raw", [[], None, {"failover": []}, {"failover": None}])
def test_config_requires_objects(tmp_path, raw):
    with pytest.raises(ConfigError, match="object"):
        load_config(_config_path(tmp_path, raw))


def test_config_preserves_budget_defaults(tmp_path):
    raw = {"nodes": [{"name": "test", "type": "tavily"}]}
    cfg = load_config(_config_path(tmp_path, raw))
    assert cfg.search_timeout_seconds is cfg.extract_timeout_seconds is None
    raw["failover"] = {"breaker_seconds": 0, "search_timeout_seconds": 2, "extract_timeout_seconds": 3}
    cfg = load_config(_config_path(tmp_path, raw))
    assert cfg.breaker_seconds == 0
    assert cfg.search_timeout_seconds == 2 and cfg.extract_timeout_seconds == 3


@pytest.fixture
def tool_server(monkeypatch):
    monkeypatch.setattr(config_loader, "load_config", lambda: AppConfig(60, []))
    previous = sys.modules.pop("server", None)
    server = importlib.import_module("server")
    yield server
    sys.modules.pop("server", None)
    if previous is not None:
        sys.modules["server"] = previous


@pytest.mark.parametrize("query", ["", " ", "\n\t"])
def test_search_rejects_blank_query(tool_server, query):
    node = FakeNode("node")
    tool_server._router = SearchRouter([node])
    with pytest.raises(ToolError, match="query"):
        asyncio.run(tool_server.mcp.call_tool("search", {"query": query}))
    assert node.calls == 0 and tool_server._router._fail_at == {}


@pytest.mark.parametrize("url", ["", "/path", "example.com", "ftp://example.com", "https:///path", "https://a b", "https://a:bad", "https://[", " https://a", "https://a\n/path"])
def test_extract_rejects_invalid_url(tool_server, url):
    node = FakeNode("node")
    tool_server._extract_router = ExtractRouter([node])
    with pytest.raises(ToolError, match="url"):
        asyncio.run(tool_server.mcp.call_tool("extract", {"url": url}))
    assert node.calls == 0 and tool_server._extract_router._fail_at == {}


@pytest.mark.parametrize("kind", ["search", "extract"])
@pytest.mark.parametrize("timeout", [False, True])
def test_tools_raise_explicit_errors(tool_server, kind, timeout):
    node = FakeNode("node", blocked=timeout, error=not timeout)
    router = _router(kind, [node], total_timeout=0.03 if timeout else None)
    if kind == "search":
        tool_server._router = router
        arguments = {"query": "test"}
    else:
        tool_server._extract_router = router
        arguments = {"url": "https://example.test/a"}
    with pytest.raises(ToolError, match="总预算" if timeout else "所有节点"):
        asyncio.run(tool_server.mcp.call_tool(kind, arguments))


def test_tool_reports_no_results(tool_server):
    tool_server._router = SearchRouter([FakeNode("empty", empty=True)])
    result = asyncio.run(tool_server.mcp.call_tool("search", {"query": "test"}))
    assert not result.is_error
    assert "共 0 条结果" in result.content[0].text
    assert "未找到相关结果" in result.content[0].text


def test_all_errors_still_fail():
    router = SearchRouter([FakeNode("failed", error=True)])
    with pytest.raises(AllProvidersFailedError):
        asyncio.run(router.search("test"))
