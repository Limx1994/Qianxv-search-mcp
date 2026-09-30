"""网页抓取（extract）故障转移与配置加载的单元测试（mock）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from config_loader import ConfigError, NodeConfig, load_config
from providers import build_extract_providers
from providers.base import ProviderError
from providers.extract_base import ExtractProvider, ExtractResult
from providers.tavily_extract import TavilyExtractProvider
from search_router import AllProvidersFailedError, ExtractRouter


class FakeExtractProvider(ExtractProvider):
    """可控的假抓取节点：可注入结果或抛错。"""

    def __init__(self, name, result=None, error=None):
        self.name = name
        self.result = result
        self.error = error
        self.calls = 0

    async def extract(self, url: str) -> ExtractResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def run(coro):
    return asyncio.run(coro)


def _make_result(source, url="https://example.com/a"):
    return ExtractResult(
        title="Test Page", url=url, content="# Hello\n正文内容", source=source
    )


def test_extract_failover_switches_to_next_node():
    p1 = FakeExtractProvider(
        "bad", error=ProviderError("bad", "HTTP 422")
    )
    p2 = FakeExtractProvider("good", result=_make_result("good"))
    router = ExtractRouter([p1, p2], breaker_seconds=60)
    result, hit = run(router.extract("https://example.com/a"))
    assert hit == "good"
    assert result.content.startswith("# Hello")
    assert p1.calls == 1
    assert p2.calls == 1


@pytest.mark.parametrize(
    ("content", "title"),
    [("# Real Page\nBody", "Real Page"), ("Body without heading", "")],
)
def test_tavily_extract_title_from_markdown(monkeypatch, content, title):
    node = NodeConfig(
        name="tavily", type="tavily_extract", enabled=True,
        api_key="test-key", timeout_seconds=5,
        options={"endpoint": "https://example.test/extract"},
    )
    provider = TavilyExtractProvider(node)

    async def fake_post(_url, _body):
        return {"results": [{
            "url": "https://example.test/page", "raw_content": content,
        }]}

    monkeypatch.setattr(provider, "_post_json", fake_post)
    result = run(provider.extract("https://example.test/page"))
    assert result.title == title
    assert result.content == content


def test_extract_all_providers_failed_raises():
    p1 = FakeExtractProvider("a", error=ProviderError("a", "timeout"))
    p2 = FakeExtractProvider("b", error=ProviderError("b", "HTTP 429"))
    router = ExtractRouter([p1, p2], breaker_seconds=60)
    with pytest.raises(AllProvidersFailedError) as excinfo:
        run(router.extract("https://example.com/a"))
    assert "a" in str(excinfo.value) and "b" in str(excinfo.value)


def test_extract_empty_content_fails_over():
    # 空内容由 provider 层抛 ProviderError（如 Tavily 空 results）
    p2 = FakeExtractProvider(
        "tavily_like", error=ProviderError("tavily_like", "empty results")
    )
    p3 = FakeExtractProvider("good", result=_make_result("good"))
    router2 = ExtractRouter([p2, p3], breaker_seconds=60)
    _, hit = run(router2.extract("https://example.com/a"))
    assert hit == "good"


def test_extract_breaker_skips_recently_failed_node():
    p1 = FakeExtractProvider(
        "bad", error=ProviderError("bad", "HTTP 500")
    )
    p2 = FakeExtractProvider("good", result=_make_result("good"))
    router = ExtractRouter([p1, p2], breaker_seconds=3600)
    _, hit = run(router.extract("https://example.com/a"))
    assert hit == "good"
    assert p1.calls == 1
    _, hit = run(router.extract("https://example.com/a"))
    assert hit == "good"
    assert p1.calls == 1  # 熔断打开，未再调用
    router.breaker_seconds = 0
    _, hit = run(router.extract("https://example.com/a"))
    assert hit == "good"
    assert p1.calls == 2


def test_extract_unexpected_exception_fails_over():
    p1 = FakeExtractProvider("boom", error=RuntimeError("unexpected"))
    p2 = FakeExtractProvider("good", result=_make_result("good"))
    router = ExtractRouter([p1, p2], breaker_seconds=60)
    _, hit = run(router.extract("https://example.com/a"))
    assert hit == "good"


def _write_tmp_config(tmp_path: Path, extract_nodes) -> Path:
    cfg = {
        "failover": {"breaker_seconds": 5},
        "nodes": [
            {
                "name": "n1",
                "type": "anysearch",
                "enabled": True,
                "api_key": "k1",
                "timeout_seconds": 3,
                "options": {"endpoint": "https://e1"},
            }
        ],
        "extract_nodes": extract_nodes,
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return path


def test_extract_config_build_and_disabled_filter(tmp_path):
    nodes = [
        {
            "name": "ex1",
            "type": "anysearch_extract",
            "enabled": True,
            "api_key": "k1",
            "timeout_seconds": 15,
            "options": {"endpoint": "https://e1"},
        },
        {
            "name": "ex2",
            "type": "tavily_extract",
            "enabled": False,
            "api_key": "k2",
            "timeout_seconds": 20,
            "options": {"endpoint": "https://e2"},
        },
    ]
    cfg = load_config(_write_tmp_config(tmp_path, nodes))
    assert [n.name for n in cfg.extract_nodes] == ["ex1", "ex2"]
    providers = build_extract_providers(cfg)
    assert [p.name for p in providers] == ["ex1"]


def test_extract_config_rejects_bad_type(tmp_path):
    nodes = [
        {
            "name": "x",
            "type": "qianfan",
            "enabled": True,
            "api_key": "k",
            "timeout_seconds": 3,
            "options": {},
        }
    ]
    with pytest.raises(ConfigError):
        load_config(_write_tmp_config(tmp_path, nodes))


def test_extract_config_optional_section(tmp_path):
    cfg = {
        "failover": {"breaker_seconds": 5},
        "nodes": [
            {
                "name": "n1",
                "type": "anysearch",
                "enabled": True,
                "api_key": "k1",
                "timeout_seconds": 3,
                "options": {},
            }
        ],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    loaded = load_config(path)
    assert loaded.extract_nodes == []
