"""本机搜索、HTML 抓取与故障转移的无密钥验证。"""

from __future__ import annotations

import asyncio
import importlib
import json
import socket
import sys
import threading
from pathlib import Path

import httpx
import pytest

import config_loader
from config_loader import AppConfig, NodeConfig, load_config
from providers import local_extract, local_search
from providers import build_extract_providers, build_providers
from providers.base import ProviderError, SearchResult
from search_router import (
    AllProvidersFailedError, ExtractRouter, RequestTimeoutError, SearchRouter,
)

HTML = (
    '<html><head><meta charset="utf-8"><title>本机文章</title></head>'
    '<body><nav>导航菜单</nav><article><h1>本机文章</h1><p>'
    + '这是一段公开网页的正文，用于验证本机抓取与中文提取。' * 20
    + '正文中可以继续阅读<a href="https://example.test/doc">参考文档</a>获取更多内容。</p>'
    '</article></body></html>'
).encode("utf-8")


def _node(kind, options=None, timeout=None):
    return NodeConfig(
        name=kind, type=kind, enabled=True, api_key="",
        timeout_seconds=timeout or (15 if kind == "local_extract" else 10),
        options=options if options is not None else (
            {"backend": "duckduckgo"} if kind == "local_search" else {}
        ),
    )


def _ddgs(monkeypatch, rows=None, error=None):
    calls = []

    class FakeDDGS:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def text(self, query, **kwargs):
            calls.append((query, kwargs, threading.get_ident()))
            if error is not None:
                raise error
            return rows

    monkeypatch.setattr(local_search, "DDGS", FakeDDGS)
    return calls


@pytest.fixture
def public_dns(monkeypatch):
    calls = []

    async def resolve(self, host, port, **kwargs):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolve)
    return calls


def test_local_search_results(monkeypatch):
    rows = [{"title": f"文章 {i}", "href": f"https://example.test/{i}",
             "body": "中文摘要"} for i in range(4)]
    calls = _ddgs(monkeypatch, rows)
    provider = local_search.LocalSearchProvider(_node("local_search"))
    result = asyncio.run(provider.search("中文搜索", 2))
    assert len(result) == 2
    assert result[0] == SearchResult("文章 0", "https://example.test/0", "中文摘要", "local_search")
    assert calls[0] == {"timeout": 10.0}
    assert calls[1][0] == "中文搜索"
    assert calls[1][1] == {"max_results": 2, "backend": "duckduckgo",
                           "region": "cn-zh", "safesearch": "moderate"}
    assert calls[1][2] != threading.get_ident()


@pytest.mark.parametrize("backend", ["auto,duckduckgo", "bing", "unknown", "", None, 3,
                                     "duckduckgo,unknown"])
def test_local_search_bad_backend(monkeypatch, backend):
    calls = _ddgs(monkeypatch, [])
    provider = local_search.LocalSearchProvider(_node("local_search", {"backend": backend}))
    with pytest.raises(ProviderError, match="unsupported or disabled"):
        asyncio.run(provider.search("query", 1))
    assert not calls


@pytest.mark.parametrize("region", ["", None, 3])
def test_local_search_bad_region(monkeypatch, region):
    calls = _ddgs(monkeypatch, [])
    provider = local_search.LocalSearchProvider(_node("local_search", {"region": region}))
    with pytest.raises(ProviderError, match="region must"):
        asyncio.run(provider.search("query", 1))
    assert not calls


@pytest.mark.parametrize("rows", [None, {}, [None], [{"title": "a"}],
                                  [{"title": "a", "href": "file:///a", "body": "b"}]])
def test_local_search_bad_results(monkeypatch, rows):
    _ddgs(monkeypatch, rows)
    provider = local_search.LocalSearchProvider(_node("local_search"))
    with pytest.raises(ProviderError):
        asyncio.run(provider.search("query", 1))


def test_local_search_failure(monkeypatch):
    _ddgs(monkeypatch, error=RuntimeError("private-proxy-detail"))
    provider = local_search.LocalSearchProvider(_node("local_search"))
    with pytest.raises(ProviderError, match="RuntimeError") as caught:
        asyncio.run(provider.search("query", 1))
    assert "private-proxy-detail" not in str(caught.value)


class CloudFailure:
    name = "cloud"
    timeout = 0.02

    def __init__(self, mode):
        self.mode = mode

    async def search(self, *args):
        if self.mode == "empty":
            return []
        if self.mode == "timeout":
            await asyncio.sleep(1)
        raise ProviderError(self.name, "cloud unavailable")

    async def extract(self, *args):
        if self.mode == "timeout":
            await asyncio.sleep(1)
        raise ProviderError(self.name, "cloud unavailable")


@pytest.mark.parametrize("mode", ["error", "timeout", "empty"])
def test_local_search_failover(monkeypatch, mode):
    _ddgs(monkeypatch, [{"title": "a", "href": "https://example.test/a", "body": "b"}])
    provider = local_search.LocalSearchProvider(_node("local_search"))
    router = SearchRouter([CloudFailure(mode), provider])
    result, hit = asyncio.run(router.search("query", 1))
    assert result and hit == provider.name
    assert ("cloud" in router._fail_at) == (mode != "empty")


def test_local_search_empty(monkeypatch):
    _ddgs(monkeypatch, [])
    provider = local_search.LocalSearchProvider(_node("local_search"))
    router = SearchRouter([provider])
    assert asyncio.run(router.search("query")) == ([], provider.name)
    assert router._fail_at == {}


def test_local_extract_html(public_dns):
    requests = []
    provider = local_extract.LocalExtractProvider(_node(
        "local_extract", {"trusted_dns_networks": []},
    ))
    provider.api_key = "unused-test-key"

    def respond(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=HTML)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider._http_client = client
            first = await provider.extract("https://example.test/article")
            second = await provider.extract("https://example.test/article")
            assert first == second and not client.is_closed
            return first

    result = asyncio.run(exercise())
    assert result.title == "本机文章"
    assert result.url == "https://example.test/article"
    assert result.source == provider.name
    assert "公开网页的正文" in result.content and "导航菜单" not in result.content
    assert "https://example.test/doc" in result.content
    assert public_dns == []
    assert requests[0].url.host == "example.test"
    assert requests[0].headers["host"] == "example.test"
    assert "authorization" not in requests[0].headers


@pytest.mark.parametrize("mode", ["error", "timeout"])
def test_local_extract_failover(public_dns, mode):
    provider = local_extract.LocalExtractProvider(_node("local_extract"))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, headers={"content-type": "text/html"}, content=HTML)
        )) as client:
            provider._http_client = client
            router = ExtractRouter([CloudFailure(mode), provider])
            result, hit = await router.extract("https://example.test/a")
            assert "cloud" in router._fail_at
            return result, hit

    result, hit = asyncio.run(exercise())
    assert result.content and hit == provider.name


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254",
                                     "100.64.0.1", "::1", "fc00::1", "224.0.0.1"])
def test_local_extract_private_ip(address):
    provider = local_extract.LocalExtractProvider(_node("local_extract"))
    host = f"[{address}]" if ":" in address else address
    with pytest.raises(ProviderError, match="non-public address"):
        asyncio.run(provider._public_target(f"https://{host}/a"))


@pytest.mark.parametrize("url", ["file:///tmp/a", "https://user:pass@example.test/a",
                                 "http://localhost/a", "http://a.localhost/a", "http://", "http://[bad"])
def test_local_extract_invalid_url(public_dns, url):
    provider = local_extract.LocalExtractProvider(_node("local_extract"))
    with pytest.raises(ProviderError):
        asyncio.run(provider._public_target(url))
    assert not public_dns


def test_local_extract_no_dns_probe(monkeypatch):
    async def resolve(self, *args, **kwargs):
        raise socket.gaierror("not found")

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolve)
    provider = local_extract.LocalExtractProvider(_node(
        "local_extract", {"trusted_dns_networks": []},
    ))
    target = asyncio.run(provider._public_target("https://example.test/a"))
    assert target.host == "example.test"


def test_local_extract_redirect(public_dns):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://other.test/final", "set-cookie": "secret=value"})
        assert "cookie" not in request.headers
        return httpx.Response(200, headers={"content-type": "text/html"}, content=HTML)

    provider = local_extract.LocalExtractProvider(_node(
        "local_extract", {"trusted_dns_networks": []},
    ))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider._http_client = client
            return await provider.extract("https://example.test/start")

    result = asyncio.run(exercise())
    assert result.url == "https://other.test/final"
    assert public_dns == []
    assert [r.headers["host"] for r in requests] == ["example.test", "other.test"]


def test_local_extract_bad_redirect(public_dns):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://localhost/private"})

    provider = local_extract.LocalExtractProvider(_node("local_extract"))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider._http_client = client
            await provider.extract("https://example.test/start")

    with pytest.raises(ProviderError, match="non-public"):
        asyncio.run(exercise())
    assert len(calls) == 1


@pytest.mark.parametrize("status,headers,body,reason", [
    (403, {}, b"denied", "HTTP 403"),
    (200, {"content-type": "application/pdf"}, b"pdf", "not HTML"),
    (200, {"content-type": "text/html"}, b"", "empty extracted"),
    (200, {"content-type": "text/html", "content-length": "5242881"}, HTML, "5 MiB"),
    (200, {"content-type": "text/html", "content-length": "bad"}, HTML, "Content-Length"),
    (302, {}, b"", "without Location"),
    (302, {"location": "/loop"}, b"", "too many redirects"),
])
def test_local_extract_bad_response(public_dns, status, headers, body, reason):
    provider = local_extract.LocalExtractProvider(_node("local_extract"))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(status, headers=headers, content=body)
        )) as client:
            provider._http_client = client
            await provider.extract("https://example.test/a")

    with pytest.raises(ProviderError, match=reason):
        asyncio.run(exercise())


def test_local_extract_stream_limit(public_dns, monkeypatch):
    monkeypatch.setattr(local_extract, "_MAX_BYTES", 100)

    class Chunks(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"a" * 80
            yield b"b" * 80

        async def aclose(self):
            self.closed = True

    stream = Chunks()
    provider = local_extract.LocalExtractProvider(_node("local_extract"))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, headers={"content-type": "text/html"}, stream=stream)
        )) as client:
            provider._http_client = client
            await provider.extract("https://example.test/a")

    with pytest.raises(ProviderError, match="5 MiB"):
        asyncio.run(exercise())
    assert stream.closed


@pytest.mark.parametrize("error", [httpx.ReadTimeout("slow"), httpx.ConnectError("offline")])
def test_local_extract_network_error(public_dns, error):
    def respond(request):
        raise error

    provider = local_extract.LocalExtractProvider(_node("local_extract"))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider._http_client = client
            await provider.extract("https://example.test/a")

    with pytest.raises(ProviderError, match="timeout|network error"):
        asyncio.run(exercise())


def test_local_extract_thread(public_dns, monkeypatch):
    threads = []
    parse = local_extract.extract_html

    def capture(*args, **kwargs):
        threads.append(threading.get_ident())
        return parse(*args, **kwargs)

    monkeypatch.setattr(local_extract, "extract_html", capture)
    provider = local_extract.LocalExtractProvider(_node("local_extract"))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, headers={"content-type": "text/html"}, content=HTML)
        )) as client:
            provider._http_client = client
            await provider.extract("https://example.test/a")

    asyncio.run(exercise())
    assert threads and threads[0] != threading.get_ident()


def test_local_config_defaults(tmp_path):
    raw = {
        "nodes": [{"name": "local", "type": "local_search"}],
        "extract_nodes": [{"name": "html", "type": "local_extract"}],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    cfg = load_config(path)
    search = build_providers(cfg)
    extract = build_extract_providers(cfg)
    assert isinstance(search[0], local_search.LocalSearchProvider)
    assert isinstance(extract[0], local_extract.LocalExtractProvider)
    assert search[0].api_key == extract[0].api_key == ""
    assert search[0].timeout == 10 and extract[0].timeout == 15
    assert SearchRouter(search).total_timeout == 10
    assert ExtractRouter(extract).total_timeout == 15
    cfg.nodes[0].enabled = cfg.extract_nodes[0].enabled = False
    assert build_providers(cfg) == build_extract_providers(cfg) == []


def test_local_template():
    path = Path(__file__).resolve().parents[1] / "config.example.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    cfg = load_config(path)
    assert cfg.nodes[-1].type == "local_search"
    assert cfg.extract_nodes[-1].type == "local_extract"
    assert cfg.extract_nodes[-1].options == {}
    assert all(node["api_key"] == "" for section in ("nodes", "extract_nodes")
               for node in raw[section])
    assert cfg.search_timeout_seconds is cfg.extract_timeout_seconds is None


@pytest.fixture
def server_cache():
    loaded = "server" in sys.modules
    yield
    if not loaded:
        sys.modules.pop("server", None)


def test_local_server_paging(public_dns, monkeypatch, server_cache):
    cfg = AppConfig(60, [_node("local_search")], [_node("local_extract")])
    monkeypatch.setattr(config_loader, "load_config", lambda: cfg)
    server = importlib.import_module("server")
    monkeypatch.setattr(server, "_router", SearchRouter(build_providers(cfg)))
    monkeypatch.setattr(server, "_extract_router", ExtractRouter(build_extract_providers(cfg)))
    server._extract_cache.clear()
    _ddgs(monkeypatch, [{"title": "文章", "href": "https://example.test/a", "body": "正文"}])
    body = ("<html><head><meta charset='utf-8'><title>长文章</title></head><body><article>"
            + "".join(f"<p>段落 {i}。" + "这是需要完整分页的正文内容。" * 30 + "</p>" for i in range(25))
            + "</article></body></html>").encode()
    requests, clients = [], []
    client_type = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=body)

    def make_client(**kwargs):
        client = client_type(transport=httpx.MockTransport(respond), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(server.httpx, "AsyncClient", make_client)

    async def exercise():
        async with server._lifespan(server.mcp):
            async with server._lifespan(server.mcp):
                output = await server.search("中文", 1)
                assert "来源节点: local_search" in output
                tool = await server.mcp.call_tool("extract", {"url": "https://example.test/a"})
                output = tool.content[0].text
            assert all(not client.is_closed for client in clients)
            parts = []
            while True:
                headers, content = output.split("---\n", 1)
                parts.append(content)
                fields = dict(line.split(": ", 1) for line in headers.splitlines() if ": " in line)
                if fields["has_more"] == "false":
                    break
                output = await server.extract(
                    "https://example.test/a", int(fields["next_offset"]), fields["snapshot_id"],
                )
            return "".join(parts), fields

    content, fields = asyncio.run(exercise())
    assert len(content) == int(fields["total_chars"]) > 8000
    assert "段落 0。" in content and "段落 24。" in content
    assert len(requests) == 1 and len(clients) == 2
    assert all(client.is_closed for client in clients)
    assert all(provider._http_client is None for provider in
               (*server._router.providers, *server._extract_router.providers))
    assert not server._extract_cache._items


@pytest.mark.parametrize("kind", ["search", "extract"])
def test_local_budget_stops_fallback(public_dns, monkeypatch, kind):
    calls = _ddgs(monkeypatch, [])
    cloud = CloudFailure("timeout")
    local = (local_search.LocalSearchProvider(_node("local_search")) if kind == "search"
             else local_extract.LocalExtractProvider(_node("local_extract")))
    cls = SearchRouter if kind == "search" else ExtractRouter
    router = cls([cloud, local], total_timeout=0.001)
    with pytest.raises(RequestTimeoutError):
        asyncio.run(getattr(router, kind)("https://example.test/a"))
    assert not calls and not public_dns


def test_local_extract_parse_failure(public_dns, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("invalid document")

    monkeypatch.setattr(local_extract, "extract_html", broken)
    provider = local_extract.LocalExtractProvider(_node("local_extract"))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, headers={"content-type": "text/html"}, content=HTML)
        )) as client:
            provider._http_client = client
            with pytest.raises(AllProvidersFailedError, match="HTML extraction failed"):
                await ExtractRouter([provider]).extract("https://example.test/a")

    asyncio.run(exercise())
