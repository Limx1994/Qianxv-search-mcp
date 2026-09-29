"""故障转移编排与配置加载的单元测试（mock，不发起真实请求）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from config_loader import ConfigError, load_config
from providers import build_providers
from providers.base import (
    ProviderError,
    SearchProvider,
    SearchResult,
)
from search_router import AllProvidersFailedError, SearchRouter


class FakeProvider(SearchProvider):
    """可控的假节点：可注入结果或抛错。"""

    def __init__(self, name, results=None, error=None):
        self.name = name
        self.results = results or []
        self.error = error
        self.calls = 0

    async def search(self, query: str, max_results: int):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.results


def _make_results(source, n=1):
    return [
        SearchResult(title=f"t{i}", url=f"https://x/{i}", snippet=f"s{i}",
                     source=source)
        for i in range(n)
    ]


def run(coro):
    return asyncio.run(coro)


def test_failover_switches_to_next_node():
    p1 = FakeProvider("bad", error=ProviderError("bad", "HTTP 500"))
    p2 = FakeProvider("good", results=_make_results("good", 2))
    router = SearchRouter([p1, p2], breaker_seconds=60)
    results, hit = run(router.search("query", 5))
    assert hit == "good"
    assert len(results) == 2
    assert p1.calls == 1
    assert p2.calls == 1


def test_all_providers_failed_raises():
    p1 = FakeProvider("a", error=ProviderError("a", "timeout"))
    p2 = FakeProvider("b", error=ProviderError("b", "HTTP 429"))
    router = SearchRouter([p1, p2], breaker_seconds=60)
    with pytest.raises(AllProvidersFailedError) as excinfo:
        run(router.search("query", 5))
    assert "a" in str(excinfo.value) and "b" in str(excinfo.value)


def test_empty_results_triggers_failover():
    p1 = FakeProvider("empty", results=[])
    p2 = FakeProvider("ok", results=_make_results("ok", 1))
    router = SearchRouter([p1, p2], breaker_seconds=60)
    _, hit = run(router.search("query", 5))
    assert hit == "ok"
    assert p1.calls == 1


def test_breaker_skips_recently_failed_node():
    p1 = FakeProvider("bad", error=ProviderError("bad", "HTTP 500"))
    p2 = FakeProvider("good", results=_make_results("good", 1))
    router = SearchRouter([p1, p2], breaker_seconds=3600)
    _, hit = run(router.search("q1", 5))
    assert hit == "good"
    assert p1.calls == 1
    # 第二次请求：熔断打开，p1 被跳过
    _, hit = run(router.search("q2", 5))
    assert hit == "good"
    assert p1.calls == 1  # 未再调用
    # 熔断窗口归零后恢复调用
    router.breaker_seconds = 0
    _, hit = run(router.search("q3", 5))
    assert hit == "good"
    assert p1.calls == 2


def test_unexpected_exception_also_fails_over():
    p1 = FakeProvider("boom", error=RuntimeError("unexpected"))
    p2 = FakeProvider("good", results=_make_results("good", 1))
    router = SearchRouter([p1, p2], breaker_seconds=60)
    _, hit = run(router.search("query", 5))
    assert hit == "good"


def test_no_providers_raises():
    router = SearchRouter([], breaker_seconds=60)
    with pytest.raises(AllProvidersFailedError):
        run(router.search("query", 5))


def _write_tmp_config(tmp_path: Path, nodes) -> Path:
    cfg = {"failover": {"breaker_seconds": 5}, "nodes": nodes}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_config_and_disabled_filter(tmp_path):
    nodes = [
        {"name": "n1", "type": "anysearch", "enabled": True,
         "api_key": "k1", "timeout_seconds": 3,
         "options": {"endpoint": "https://e1"}},
        {"name": "n2", "type": "tavily", "enabled": False,
         "api_key": "k2", "timeout_seconds": 8,
         "options": {"endpoint": "https://e2"}},
        {"name": "n3", "type": "qianfan", "enabled": True,
         "api_key": "k3", "timeout_seconds": 8,
         "options": {"endpoint": "https://e3"}},
        {"name": "n4", "type": "brightdata", "enabled": True,
         "api_key": "k4", "timeout_seconds": 60,
         "options": {"endpoint": "https://e4", "engine": "google"}},
    ]
    cfg = load_config(_write_tmp_config(tmp_path, nodes))
    assert [n.name for n in cfg.nodes] == ["n1", "n2", "n3", "n4"]
    assert cfg.breaker_seconds == 5
    providers = build_providers(cfg)
    # 禁用节点被跳过，顺序保持
    assert [p.name for p in providers] == ["n1", "n3", "n4"]


def test_load_config_rejects_bad_type(tmp_path):
    nodes = [{"name": "x", "type": "unknown", "enabled": True,
              "api_key": "k", "timeout_seconds": 3, "options": {}}]
    with pytest.raises(ConfigError):
        load_config(_write_tmp_config(tmp_path, nodes))


def test_load_config_rejects_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.json")
