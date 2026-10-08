"""云节点请求契约、解析与 HTTP 失败路径；所有请求均使用 MockTransport。"""

import asyncio
import json

import httpx
import pytest

from config_loader import NodeConfig
from providers.anysearch import AnySearchProvider
from providers.anysearch_extract import AnySearchExtractProvider
from providers.base import ProviderError
from providers.qianfan import QianfanProvider
from providers.tavily import TavilyProvider
from providers.tavily_extract import TavilyExtractProvider
from providers.volc_ark import VolcArkProvider


URL = "https://example.test/api"
SEARCH_TYPES = [AnySearchProvider, QianfanProvider, VolcArkProvider, TavilyProvider]
EXTRACT_TYPES = [AnySearchExtractProvider, TavilyExtractProvider]


def make_provider(cls, key="dummy", endpoint=URL):
    return cls(NodeConfig("mock", "mock", True, key, 2, {
        "endpoint": endpoint, "zone": "web", "language": "zh", "tag": "test",
    }))


@pytest.mark.parametrize("cls", SEARCH_TYPES + EXTRACT_TYPES)
@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("case", ["ok", "timeout", "network", "status", "json", "shape"])
def test_http_failure_paths(monkeypatch, cls, shared, case):
    provider = make_provider(cls, key="")
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["content-type"] == "application/json"
        assert "authorization" not in request.headers
        if case == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if case == "network":
            raise httpx.ConnectError("offline", request=request)
        if case == "status":
            return httpx.Response(429, text="quota")
        if case == "json":
            return httpx.Response(200, text="broken")
        return httpx.Response(200, json=[] if case == "shape" else {"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    if shared:
        provider._http_client = client
    else:
        def factory(**kwargs):
            assert kwargs["timeout"] == 2
            return client
        monkeypatch.setattr(httpx, "AsyncClient", factory)

    async def exercise():
        try:
            if case == "ok":
                assert await provider._post_json(URL, {"q": "中文"}) == {"ok": True}
                assert json.loads(requests[0].content) == {"q": "中文"}
            else:
                reason = {"timeout": "timeout", "network": "network error",
                          "status": "HTTP 429", "json": "invalid JSON",
                          "shape": "unexpected response shape"}[case]
                with pytest.raises(ProviderError, match=reason) as error:
                    await provider._post_json(URL, {})
                assert error.value.provider == "mock"
            assert client.is_closed is not shared
        finally:
            await client.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("headers", [None, {"X-Test": "yes"}])
def test_get_request_contract(headers):
    provider = make_provider(AnySearchProvider)
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.params["q"] == "中文"
        assert request.headers["authorization"] == "Bearer dummy"
        assert request.headers.get("x-test") == ("yes" if headers else None)
        return httpx.Response(200, json={"ok": True})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider._http_client = client
            assert await provider._get_json(URL, {"q": "中文"}, headers) == {"ok": True}
    asyncio.run(exercise())
    assert len(requests) == 1


@pytest.mark.parametrize("cls", SEARCH_TYPES + EXTRACT_TYPES)
def test_missing_endpoint(cls):
    provider = make_provider(cls, endpoint="")
    call = provider.search("q", 1) if cls in SEARCH_TYPES else provider.extract(URL)
    with pytest.raises(ProviderError, match="endpoint not configured"):
        asyncio.run(call)


@pytest.mark.parametrize("cls", [QianfanProvider, VolcArkProvider, TavilyProvider] + EXTRACT_TYPES)
def test_missing_key(cls):
    provider = make_provider(cls, key="")
    call = provider.search("q", 1) if cls in SEARCH_TYPES else provider.extract(URL)
    with pytest.raises(ProviderError, match="api_key required"):
        asyncio.run(call)


@pytest.mark.parametrize("cls", SEARCH_TYPES)
@pytest.mark.parametrize("case", ["full", "empty", "error"])
def test_search_response_paths(cls, case):
    provider = make_provider(cls)
    item = {"title": "标题", "url": URL, "snippet": "摘" * 600, "content": "正文"}
    if cls is AnySearchProvider:
        payload = {"data": {"results": [None, item, {"content": "fallback"}, {}]}}
        empty, error = {}, {"code": 2, "message": "quota"}
    elif cls is QianfanProvider:
        payload = {"references": [None, item, {"content": "fallback"}, {}]}
        empty, error = {}, {"error_code": 2, "error_message": "quota"}
    elif cls is VolcArkProvider:
        payload = {"Result": {"WebResults": [None, {}, {
            "Title": "标题", "Url": URL, "Snippet": "摘" * 600,
        }, {"Url": URL, "Summary": "fallback"}, {"Url": URL}]}}
        empty = {"Result": {}}
        error = {"ResponseMetadata": {"Error": {"Code": 2, "Message": "quota"}}}
    else:
        payload = {"results": [None, {**item, "content": "摘" * 600}, {}]}
        empty, error = {}, {"detail": "quota"}
    body = []

    def respond(request):
        body.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer dummy"
        return httpx.Response(200, json={"full": payload, "empty": empty, "error": error}[case])

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider._http_client = client
            if case == "error":
                with pytest.raises(ProviderError, match="biz error"):
                    await provider.search("字" * 150, 20)
            else:
                rows = await provider.search("字" * 150, 20)
                if case == "empty":
                    assert rows == []
                else:
                    assert rows[0].title == "标题" and rows[0].url == URL
                    assert rows[0].snippet == "摘" * 500
                    assert all(row.source == "mock" for row in rows)
                    assert rows[-1].snippet == ""
                    if cls is not TavilyProvider:
                        assert rows[1].snippet == "fallback"
            if cls is AnySearchProvider:
                assert body[0] == {"query": "字" * 150, "max_results": 10,
                                   "format": "json", "zone": "web",
                                   "language": "zh", "tag": "test"}
            elif cls is QianfanProvider:
                assert body[0]["messages"][0]["content"] == "字" * 144
                assert body[0]["resource_type_filter"] == [{"type": "web", "top_k": 20}]
            elif cls is VolcArkProvider:
                assert body[0]["Query"] == "字" * 100 and body[0]["Count"] == 20
            else:
                assert body[0] == {"query": "字" * 150, "max_results": 20, "search_depth": "basic"}
    asyncio.run(exercise())


def test_volc_null_result(monkeypatch):
    provider = make_provider(VolcArkProvider)
    async def respond(*args):
        return {}
    monkeypatch.setattr(provider, "_post_json", respond)
    with pytest.raises(ProviderError, match="Result is null"):
        asyncio.run(provider.search("short", 1))


@pytest.mark.parametrize("cls,payload,reason", [
    (AnySearchExtractProvider, {"code": 1}, "biz error"),
    (AnySearchExtractProvider, {}, "empty extracted"),
    (TavilyExtractProvider, {"detail": "bad"}, "biz error"),
    (TavilyExtractProvider, {}, "empty results"),
    (TavilyExtractProvider, {"failed_results": [None]}, "empty results"),
    (TavilyExtractProvider, {"failed_results": [{"error": "denied"}]}, "denied"),
    (TavilyExtractProvider, {"results": [None]}, "unexpected result"),
    (TavilyExtractProvider, {"results": [{}]}, "empty extracted"),
])
def test_extract_error_payloads(monkeypatch, cls, payload, reason):
    provider = make_provider(cls)
    async def respond(*args):
        return payload
    monkeypatch.setattr(provider, "_post_json", respond)
    with pytest.raises(ProviderError, match=reason):
        asyncio.run(provider.extract(URL))


@pytest.mark.parametrize("cls", EXTRACT_TYPES)
def test_extract_request_contract(cls):
    provider = make_provider(cls)
    def respond(request):
        body = json.loads(request.content)
        if cls is AnySearchExtractProvider:
            assert body == {"url": URL}
            return httpx.Response(200, json={"data": {"content": "body"}})
        assert body == {"urls": URL, "format": "markdown"}
        return httpx.Response(200, json={"results": [{"raw_content": "# \n# Title\nbody"}]})
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider._http_client = client
            result = await provider.extract(URL)
            assert result.url == URL and result.source == "mock"
            assert result.title == ("" if cls is AnySearchExtractProvider else "Title")
            assert result.content.endswith("body")
    asyncio.run(exercise())
