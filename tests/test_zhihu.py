"""知乎全网搜索适配器单元测试（mock _get_json，不发起真实请求）。"""

from __future__ import annotations

import asyncio

import pytest

from config_loader import NodeConfig
from providers.base import ProviderError
from providers.zhihu import ZhihuProvider


def _make_node(**overrides) -> NodeConfig:
    opts = {
        "endpoint": (
            "https://developer.zhihu.com/api/v1/content/global_search"
        ),
    }
    opts.update(overrides.pop("options", {}))
    node = NodeConfig(
        name="zhihu",
        type="zhihu",
        enabled=True,
        api_key="zh-secret",
        timeout_seconds=15,
        options=opts,
    )
    for k, v in overrides.items():
        setattr(node, k, v)
    return node


def _make_provider(**overrides) -> ZhihuProvider:
    return ZhihuProvider(_make_node(**overrides))


def _ok_payload(items: list) -> dict:
    return {"Code": 0, "Message": "success", "Data": {"HasMore": False, "Items": items}}


def run(coro):
    return asyncio.run(coro)


def test_parse_items_results():
    provider = _make_provider()
    calls = []

    async def fake_get(url, params=None, headers=None):
        calls.append((url, params, headers))
        return _ok_payload([
            {
                "Title": "t1",
                "Url": "https://example.com/1",
                "ContentText": "d1",
            },
            {
                "Title": "t2",
                "Url": "https://example.com/2",
                "ContentText": "d2",
            },
        ])

    provider._get_json = fake_get
    results = run(provider.search("查询词", 5))
    assert len(results) == 2
    assert results[0].title == "t1"
    assert results[0].url == "https://example.com/1"
    assert results[0].snippet == "d1"
    assert results[0].source == "zhihu"
    # 端点与请求参数正确传递
    url, params, headers = calls[0]
    assert url == "https://developer.zhihu.com/api/v1/content/global_search"
    assert params["Query"] == "查询词"
    assert params["Count"] == 5
    assert "X-Request-Timestamp" in headers


def test_count_capped_to_20():
    provider = _make_provider()
    calls = []

    async def fake_get(url, params=None, headers=None):
        calls.append((url, params, headers))
        return _ok_payload([])

    provider._get_json = fake_get
    run(provider.search("q", 50))
    assert calls[0][1]["Count"] == 20


def test_max_results_limit():
    provider = _make_provider()

    async def fake_get(url, params=None, headers=None):
        return _ok_payload([
            {
                "Title": f"t{i}",
                "Url": f"https://example.com/{i}",
                "ContentText": "d",
            }
            for i in range(10)
        ])

    provider._get_json = fake_get
    assert len(run(provider.search("q", 3))) == 3


def test_em_tags_stripped_and_snippet_truncated():
    provider = _make_provider()

    async def fake_get(url, params=None, headers=None):
        return _ok_payload([
            {
                "Title": "t",
                "Url": "https://example.com/1",
                "ContentText": "<em>高亮</em>" + "x" * 600,
            }
        ])

    provider._get_json = fake_get
    results = run(provider.search("q", 5))
    assert results[0].snippet.startswith("高亮")
    assert "<em>" not in results[0].snippet
    assert len(results[0].snippet) == 500


def test_empty_items_returns_empty():
    provider = _make_provider()

    async def fake_get(url, params=None, headers=None):
        return _ok_payload([])

    provider._get_json = fake_get
    assert run(provider.search("q", 5)) == []


def test_biz_error_raises():
    provider = _make_provider()

    async def fake_get(url, params=None, headers=None):
        return {"Code": 1001, "Message": "quota exceeded"}

    provider._get_json = fake_get
    with pytest.raises(ProviderError) as excinfo:
        run(provider.search("q", 5))
    assert "biz error" in str(excinfo.value)


def test_missing_api_key_raises():
    provider = _make_provider(api_key="")
    with pytest.raises(ProviderError) as excinfo:
        run(provider.search("q", 5))
    assert "api_key" in str(excinfo.value)


def test_missing_endpoint_raises():
    provider = _make_provider(options={"endpoint": ""})
    with pytest.raises(ProviderError) as excinfo:
        run(provider.search("q", 5))
    assert "endpoint" in str(excinfo.value)
