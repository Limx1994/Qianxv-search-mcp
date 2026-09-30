"""服务生命周期中的 HTTP 客户端复用与关闭（无外部请求）。"""

from __future__ import annotations

import asyncio
import importlib

import httpx

import config_loader
from config_loader import AppConfig, NodeConfig


def test_server_reuses_and_closes_http_client(monkeypatch):
    cfg = AppConfig(
        breaker_seconds=60,
        nodes=[NodeConfig(
            name="search", type="tavily", enabled=True, api_key="test",
            timeout_seconds=3, options={"endpoint": "https://example.test/search"},
        )],
        extract_nodes=[NodeConfig(
            name="extract", type="anysearch_extract", enabled=True,
            api_key="test", timeout_seconds=7,
            options={"endpoint": "https://example.test/extract"},
        )],
    )
    monkeypatch.setattr(config_loader, "load_config", lambda: cfg)
    server = importlib.import_module("server")

    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{
                "title": "Title", "url": "https://example.test/a", "content": "Text",
            }]})
        return httpx.Response(200, json={"data": {
            "url": "https://example.test/a", "title": "Title", "content": "Text",
        }})

    search_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    extract_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    clients = iter((search_client, extract_client))
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: next(clients))

    async def exercise():
        search_provider = server._router.providers[0]
        extract_provider = server._extract_router.providers[0]
        async with server._lifespan(server.mcp):
            assert search_provider._http_client is search_client
            assert extract_provider._http_client is extract_client
            async with server._lifespan(server.mcp):
                assert await search_provider.search("query", 1)
                assert await extract_provider.extract("https://example.test/a")
            assert not search_client.is_closed
            assert not extract_client.is_closed
            assert await search_provider.search("query", 1)
            assert await extract_provider.extract("https://example.test/a")
            assert not search_client.is_closed
            assert not extract_client.is_closed
        assert search_provider._http_client is None
        assert extract_provider._http_client is None
        assert search_client.is_closed
        assert extract_client.is_closed

    asyncio.run(exercise())
    assert len(requests) == 4
    assert [req.extensions["timeout"]["read"] for req in requests] == [
        3, 7, 3, 7,
    ]
