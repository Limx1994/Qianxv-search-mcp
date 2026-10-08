"""标准客户端代理选择与 NO_PROXY，不使用自定义代理信任名单。"""
import asyncio

import httpx
import pytest

from config_loader import NodeConfig
from providers.local_extract import LocalExtractProvider


@pytest.mark.parametrize("bypass", [False, True])
@pytest.mark.parametrize("scheme", ["http", "https", "socks5", "socks5h"])
@pytest.mark.parametrize("variable", ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"])
def test_environment_proxy(monkeypatch, bypass, scheme, variable):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.lower(), raising=False)
    monkeypatch.setenv(variable, f"{scheme}://user:placeholder@proxy.test:1080")
    monkeypatch.setenv("NO_PROXY", "example.test" if bypass else "")
    provider = LocalExtractProvider(NodeConfig(
        "local", "local_extract", True, "", 5,
        {"trusted_dns_networks": [], "trusted_proxy_urls": []},
    ))

    async def run():
        async with provider.create_http_client() as client:
            protocol = "http" if variable == "HTTP_PROXY" else "https"
            transport = client._transport_for_url(httpx.URL(f"{protocol}://example.test/"))
            proxy = getattr(transport._pool, "_proxy_url", None)
            assert (proxy is None) == bypass
            if proxy is not None:
                assert proxy.host == b"proxy.test"
                assert proxy.port == 1080
                assert proxy.scheme == scheme.encode()
    asyncio.run(run())
