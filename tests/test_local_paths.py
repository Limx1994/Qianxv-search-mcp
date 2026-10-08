"""本机节点的输入、重定向、解析与临时客户端路径。"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from config_loader import NodeConfig
from providers import local_extract, local_search
from providers.anysearch import AnySearchProvider
from providers.base import ProviderError


def make_provider(cls, options=None):
    return cls(NodeConfig("local", "local", True, "", 1, options or {}))


@pytest.mark.parametrize("query,count", [("", 1), ("  ", 1), ("q", 0), ("q", -1)])
def test_search_input_bounds(query, count):
    provider = make_provider(local_search.LocalSearchProvider)
    with pytest.raises(ProviderError, match="invalid query"):
        asyncio.run(provider.search(query, count))


@pytest.mark.parametrize("region", ["zh", "cn-zh-extra", "1n-zh", "china-zh"])
def test_bing_region_format(region):
    provider = make_provider(local_search.LocalSearchProvider, {"backend": "bing_html", "region": region})
    with pytest.raises(ProviderError, match="country-language"):
        asyncio.run(provider.search("q", 1))


@pytest.mark.parametrize("location", [None, "/search?q=q", "https://user:pass@www.bing.com/search",
    "https://:pass@www.bing.com/search"])
def test_bing_redirect_bounds(location):
    provider = make_provider(local_search.LocalSearchProvider, {"backend": "bing_html"})
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": location} if location else {})
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider._http_client = client
            with pytest.raises(ProviderError, match="invalid Bing redirect|unsafe Bing redirect"):
                await provider.search("q", 1)
        assert len(calls) == (4 if location == "/search?q=q" else 1)
    asyncio.run(exercise())


@pytest.mark.parametrize("title,href", [("", "https://example.test"),
    ("title", "ftp://example.test"), ("title", "https:///missing")])
def test_bing_bad_parsed_result(monkeypatch, title, href):
    provider = make_provider(local_search.LocalSearchProvider)
    engine = SimpleNamespace(
        extract_results=lambda html: [html],
        post_extract_results=lambda rows: [SimpleNamespace(title=title, href=href, body="body")],
    )
    monkeypatch.setattr(local_search, "Bing", lambda **kwargs: engine)
    with pytest.raises(ProviderError, match="invalid Bing search result"):
        provider._parse_bing("html", 1)


def test_bing_temporary_client(monkeypatch):
    provider = make_provider(local_search.LocalSearchProvider, {"backend": "bing_html"})
    html = '<li class="b_algo"><h2><a href="https://example.test">Title</a></h2><p>Body</p></li>'
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=html)))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
    result = asyncio.run(provider.search("q", 1))
    assert result[0].title == "Title" and client.is_closed


@pytest.mark.parametrize("length", ["-1", "0"])
def test_extract_content_length(length):
    provider = make_provider(local_extract.LocalExtractProvider)
    response = httpx.Response(200, headers={"content-type": "text/html", "content-length": length}, content=b"")
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
            provider._http_client = client
            with pytest.raises(ProviderError, match="Content-Length" if length == "-1" else "empty extracted"):
                await provider.extract("https://example.test")
            assert response.is_closed
    asyncio.run(exercise())


@pytest.mark.parametrize("host", ["93.184.216.34", "0x5db8d822", "1572395042", "[2606:4700:4700::1111]"])
def test_extract_public_literal(host):
    provider = make_provider(local_extract.LocalExtractProvider)
    url = f"https://{host}/page#fragment"
    result = asyncio.run(provider._public_target(url))
    assert result.fragment == "" and result.path == "/page"


@pytest.mark.parametrize("metadata", [None, SimpleNamespace(title=None), SimpleNamespace(title="Title")])
def test_extract_metadata_paths(monkeypatch, metadata):
    provider = make_provider(local_extract.LocalExtractProvider)
    monkeypatch.setattr(local_extract, "extract_html", lambda *args, **kwargs: "body")
    monkeypatch.setattr(local_extract, "extract_metadata", lambda *args, **kwargs: metadata)
    result = provider._parse_html(b"html", "https://example.test")
    assert result.title == ("Title" if metadata and metadata.title else "")
    assert result.content == "body"


def test_extract_temporary_client(monkeypatch):
    provider = make_provider(local_extract.LocalExtractProvider)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "application/xhtml+xml"}, content=b"html")))
    monkeypatch.setattr(provider, "create_http_client", lambda: client)
    monkeypatch.setattr(local_extract, "extract_html", lambda *args, **kwargs: "body")
    monkeypatch.setattr(local_extract, "extract_metadata", lambda *args, **kwargs: None)
    result = asyncio.run(provider.extract("https://example.test"))
    assert result.content == "body" and client.is_closed


def test_anysearch_optional_fields(monkeypatch):
    provider = make_provider(AnySearchProvider, {"endpoint": "https://example.test"})
    async def respond(url, body):
        assert body == {"query": "q", "max_results": 1, "format": "json"}
        return {}
    monkeypatch.setattr(provider, "_post_json", respond)
    assert asyncio.run(provider.search("q", 1)) == []
