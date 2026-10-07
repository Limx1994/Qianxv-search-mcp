"""网页抓取（extract）故障转移与配置加载的单元测试（mock）。"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import config_loader
from config_loader import AppConfig, ConfigError, NodeConfig, load_config
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
        self.timeout = 10.0

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


@pytest.fixture
def extract_server(monkeypatch):
    monkeypatch.setattr(
        config_loader, "load_config", lambda: AppConfig(60, [])
    )
    previous = sys.modules.pop("server", None)
    # 重新导入以应用 mock 配置，结束后恢复原模块，避免污染其他用例。
    server = importlib.import_module("server")
    server._extract_cache.clear()
    yield server
    server._extract_cache.clear()
    sys.modules.pop("server", None)
    if previous is not None:
        sys.modules["server"] = previous


class PageRouter:
    def __init__(self, content):
        self.content = content
        self.calls = 0

    async def extract(self, url):
        self.calls += 1
        return ExtractResult("Page", url, self.content, "mock"), "mock"


def _page_parts(output):
    header, separator, body = output.partition("---\n")
    assert separator
    fields = dict(
        line.split(": ", 1) for line in header.splitlines() if ": " in line
    )
    return fields, body


@pytest.mark.parametrize("size", [7999, 8000, 8001, 16000, 16001])
def test_extract_pages_join_exactly(extract_server, monkeypatch, size):
    # 覆盖分页临界值，并混入中文和 emoji，验证偏移按字符计算且拼接无损。
    content = ("# 标题\n正文😀\n" * 2000)[:size].ljust(size, "中")
    router = PageRouter(content)
    monkeypatch.setattr(extract_server, "_extract_router", router)
    pages = []
    offset = 0
    snapshot_id = None
    while True:
        output = run(extract_server.extract(
            "https://example.test/doc", offset, snapshot_id
        ))
        fields, body = _page_parts(output)
        pages.append(body)
        assert int(fields["offset"]) == offset
        assert int(fields["end_offset"]) == offset + len(body)
        assert int(fields["total_chars"]) == size
        if fields["has_more"] == "false":
            assert fields["next_offset"] == "null"
            break
        snapshot_id = fields["snapshot_id"]
        assert snapshot_id != "null"
        offset = int(fields["next_offset"])
    assert "".join(pages) == content
    assert router.calls == 1
    assert (fields["snapshot_id"] == "null") == (size <= 8000)


def test_extract_snapshot_stays_stable(extract_server, monkeypatch):
    router = PageRouter("A" * 8000 + "original")
    monkeypatch.setattr(extract_server, "_extract_router", router)
    first, _ = _page_parts(run(extract_server.extract("https://example.test/a")))
    router.content = "B" * 8000 + "changed"
    second, body = _page_parts(run(extract_server.extract(
        "https://example.test/a", 8000, first["snapshot_id"]
    )))
    assert body == "original"
    assert second["has_more"] == "false"
    assert router.calls == 1
    _, empty = _page_parts(run(extract_server.extract(
        "https://example.test/a", 8008, first["snapshot_id"]
    )))
    assert empty == ""


def test_extract_snapshot_errors(extract_server, monkeypatch):
    router = PageRouter("x" * 8001)
    monkeypatch.setattr(extract_server, "_extract_router", router)
    url = "https://example.test/a"
    fields, _ = _page_parts(run(extract_server.extract(url)))
    snapshot_id = fields["snapshot_id"]
    with pytest.raises(ValueError, match="snapshot_id is required"):
        run(extract_server.extract(url, 1))
    with pytest.raises(ValueError, match="offset must"):
        run(extract_server.extract(url, -1, snapshot_id))
    with pytest.raises(ValueError, match="URL does not match"):
        run(extract_server.extract("https://example.test/b", 1, snapshot_id))
    with pytest.raises(ValueError, match="offset exceeds"):
        run(extract_server.extract(url, 8002, snapshot_id))
    with pytest.raises(ValueError, match="snapshot not found"):
        run(extract_server.extract(url, 1, "missing"))
    assert router.calls == 1


def test_snapshot_expiry_and_eviction(extract_server, monkeypatch):
    router = PageRouter("x" * 8001)
    monkeypatch.setattr(extract_server, "_extract_router", router)
    url = "https://example.test/a"
    first, _ = _page_parts(run(extract_server.extract(url)))
    monkeypatch.setattr(extract_server, "_SNAPSHOT_TTL", 0)
    with pytest.raises(ValueError, match="snapshot not found"):
        run(extract_server.extract(url, 8000, first["snapshot_id"]))
    monkeypatch.setattr(extract_server, "_SNAPSHOT_TTL", 600)
    ids = []
    for _ in range(33):
        fields, _ = _page_parts(run(extract_server.extract(url)))
        ids.append(fields["snapshot_id"])
    with pytest.raises(ValueError, match="snapshot not found"):
        run(extract_server.extract(url, 8000, ids[0]))
    assert _page_parts(run(extract_server.extract(url, 8000, ids[-1])))[1] == "x"
    assert len(extract_server._extract_cache._items) == 32


def test_extract_snapshot_capacity(extract_server, monkeypatch):
    router = PageRouter("x" * 1_100_000)
    monkeypatch.setattr(extract_server, "_extract_router", router)
    url = "https://example.test/a"
    first, _ = _page_parts(run(extract_server.extract(url)))
    second, _ = _page_parts(run(extract_server.extract(url)))
    with pytest.raises(ValueError, match="snapshot not found"):
        run(extract_server.extract(url, 8000, first["snapshot_id"]))
    assert _page_parts(run(extract_server.extract(
        url, 8000, second["snapshot_id"]
    )))[1] == "x" * 8000
    router.content = "x" * 2_000_001
    with pytest.raises(ValueError, match="exceeds snapshot capacity"):
        run(extract_server.extract(url))


def test_extract_tool_schema_and_errors(extract_server, monkeypatch):
    router = PageRouter("x" * 8001)
    monkeypatch.setattr(extract_server, "_extract_router", router)

    async def verify():
        tools = await extract_server.mcp.list_tools()
        schema = next(t.input_schema for t in tools if t.name == "extract")
        assert set(schema["properties"]) == {"url", "offset", "snapshot_id"}
        assert schema["properties"]["offset"]["minimum"] == 0
        with pytest.raises(ToolError):
            await extract_server.mcp.call_tool(
                "extract", {"url": "https://example.test/a", "offset": -1}
            )
        with pytest.raises(ToolError):
            await extract_server.mcp.call_tool(
                "extract", {"url": "https://example.test/a", "offset": 1}
            )

    run(verify())
    assert router.calls == 0


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


@pytest.mark.parametrize("nodes", [None, []], ids=["null", "empty"])
def test_extract_config_rejects_null_empty(tmp_path, nodes):
    with pytest.raises(
        ConfigError, match="'extract_nodes' must be a non-empty array"
    ):
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
