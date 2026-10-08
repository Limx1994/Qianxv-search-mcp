"""本机节点的 Fake-IP、Bing HTML 与真实 TLS CONNECT 回归。"""
import asyncio
import datetime
import ipaddress
import socket
import ssl

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from config_loader import NodeConfig
from providers.base import ProviderError
from providers.local_extract import LocalExtractProvider
from providers.local_search import LocalSearchProvider


def node(kind, options=None):
    return NodeConfig(kind, kind, True, "", 5, options or {})


@pytest.mark.parametrize("url,allowed", [
    ("https://example.test/article", True),
    ("https://example.test:443/article", True),
    ("http://example.test/article", True),
    ("https://example.test:444/article", True),
    ("https://localhost/article", False),
    ("https://a.localhost/article", False),
    ("https://a.localhost./article", False),
    ("https://127.181.0.87/article", False),
    ("https://127.181.0.87./article", False),
    ("https://0x7fb50057/article", False),
    ("https://2142560343/article", False),
])
def test_trusted_dns_scope(monkeypatch, url, allowed):
    async def resolve(self, host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.181.0.87", port))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolve)
    provider = LocalExtractProvider(node("local_extract", {
        "trusted_dns_networks": ["127.181.0.0/16"],
    }))
    if allowed:
        original = asyncio.run(provider._public_target(url))
        assert original.host == "example.test"
    else:
        with pytest.raises(ProviderError, match="non-public"):
            asyncio.run(provider._public_target(url))


@pytest.mark.parametrize("options", [{}, {"trusted_dns_networks": []},
    {"trusted_dns_networks": ["broken/16"], "trusted_proxy_urls": []}])
@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("port", [443, 8443])
def test_domain_keeps_system_route(monkeypatch, options, port, scheme):
    async def resolve(self, host, port, **kwargs):
        raise AssertionError("automatic HTTPS routing must not require local DNS validation")

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolve)
    provider = LocalExtractProvider(node("local_extract", options))
    original = asyncio.run(provider._public_target(f"{scheme}://example.test:{port}/article"))
    assert original.host == "example.test"


@pytest.mark.parametrize("address", ["198.18.0.50", "127.181.0.87", "10.7.8.9", "fc00::87"])
def test_auto_dns_blocks_literal_ip(monkeypatch, address):
    async def resolve(self, host, port, **kwargs):
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolve)
    provider = LocalExtractProvider(node("local_extract"))
    host = f"[{address}]" if ":" in address else address
    with pytest.raises(ProviderError, match="non-public"):
        asyncio.run(provider._public_target(f"https://{host}/article"))


def test_auto_dns_checks_redirect(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://localhost/private"})

    async def run():
        provider = LocalExtractProvider(node("local_extract"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider._http_client = client
            with pytest.raises(ProviderError, match="non-public"):
                await provider.extract("https://example.test/article")

    asyncio.run(run())
    assert len(requests) == 1 and requests[0].url.host == "example.test"


@pytest.mark.parametrize("status,mime,html,reason", [
    (403, "text/html", "denied", "HTTP 403"),
    (200, "application/json", "{}", "not HTML"),
    (200, "text/html", "<html>captcha</html>", "no parsable"),
])
def test_bing_errors(status, mime, html, reason):
    async def run():
        provider = LocalSearchProvider(node("local_search", {"backend": "bing_html"}))
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, headers={"content-type": mime}, text=html)
        )) as client:
            provider._http_client = client
            with pytest.raises(ProviderError, match=reason):
                await provider.search("中文搜索", 3)
            assert not client.is_closed
    asyncio.run(run())


def test_bing_results():
    html = '<ol>' + ''.join(
        f'<li class="b_algo"><h2><a href="https://example.test/{i}">文章{i}</a></h2>'
        '<p>中文摘要</p></li>' for i in range(4)
    ) + '</ol>'

    def respond(request):
        assert request.url.params["q"] == "中文搜索"
        assert request.url.params["mkt"] == "zh-CN"
        assert "authorization" not in request.headers
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

    async def run():
        provider = LocalSearchProvider(node("local_search", {"backend": "bing_html"}))
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider._http_client = client
            rows = await provider.search("中文搜索", 2)
            assert len(rows) == 2
            assert rows[0].title == "文章0"
            assert rows[0].url == "https://example.test/0"
            assert rows[0].snippet == "中文摘要"
    asyncio.run(run())


def test_bing_timeout():
    def respond(request):
        raise httpx.ReadTimeout("network timeout", request=request)

    async def run():
        provider = LocalSearchProvider(node("local_search", {"backend": "bing_html"}))
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider._http_client = client
            with pytest.raises(ProviderError, match="ReadTimeout"):
                await provider.search("中文查询", 3)
    asyncio.run(run())


@pytest.mark.parametrize("location,success", [
    ("https://cn.bing.com/search?q=test", True),
    ("https://127.0.0.1/search", False),
    ("http://cn.bing.com/search", False),
    ("https://cn.bing.com:444/search", False),
])
def test_bing_redirect(location, success):
    calls = []

    def respond(request):
        calls.append(request.url)
        if len(calls) == 1:
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(200, headers={"content-type": "text/html"}, text=(
            '<li class="b_algo"><h2><a href="https://example.test/">文章</a></h2><p>摘要</p></li>'
        ))

    async def run():
        provider = LocalSearchProvider(node("local_search", {"backend": "bing_html"}))
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider._http_client = client
            if success:
                assert len(await provider.search("中文查询", 3)) == 1
                assert calls[-1].host == "cn.bing.com"
            else:
                with pytest.raises(ProviderError, match="unsafe Bing redirect"):
                    await provider.search("中文查询", 3)
                assert len(calls) == 1
    asyncio.run(run())


def certificate(tmp_path, hostname, ip_san="127.0.0.1"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName(hostname), x509.IPAddress(ipaddress.ip_address(ip_san)),
            ]), False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                         serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return cert_path, key_path


@pytest.mark.parametrize("hostname", ["example.test", "wrong.test"])
@pytest.mark.parametrize("proxy_dns", [False, True, "bypass", "system"])
@pytest.mark.parametrize("proxy_scheme", ["http", "https", "socks5", "socks5h"])
def test_real_connect_tls(monkeypatch, tmp_path, hostname, proxy_dns, proxy_scheme):
    cert_path, key_path = certificate(tmp_path, hostname)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    sni, requests, connects = [], [], []
    context.set_servername_callback(lambda sock, name, ctx: sni.append(name))
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_path))
    monkeypatch.setenv("NO_PROXY", "")

    async def run():
        body = ('<html><head><title>CONNECT article</title></head><body><article><p>'
                + 'TLS certificate validation and extraction work correctly. ' * 30
                + '</p></article></body></html>').encode()

        async def upstream(reader, writer):
            try:
                requests.append(await reader.readuntil(b"\r\n\r\n"))
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
                await writer.drain()
            finally:
                writer.close()

        tls_server = await asyncio.start_server(upstream, "127.0.0.1", 0, ssl=context)
        tls_port = tls_server.sockets[0].getsockname()[1]

        async def relay(reader, writer):
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()

        async def proxy(reader, writer):
            if proxy_scheme in ("socks5", "socks5h"):
                version, methods = await reader.readexactly(2)
                assert version == 5
                await reader.readexactly(methods)
                writer.write(b"\x05\x00")
                await writer.drain()
                header = await reader.readexactly(4)
                assert header[:3] == b"\x05\x01\x00"
                if header[3] == 3:
                    size = (await reader.readexactly(1))[0]
                    host = (await reader.readexactly(size)).decode()
                else:
                    assert header[3] == 1
                    host = socket.inet_ntoa(await reader.readexactly(4))
                port = int.from_bytes(await reader.readexactly(2), "big")
                connects.append(f"CONNECT {host}:{port} HTTP/1.1".encode())
                reply = b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00"
            else:
                header = await reader.readuntil(b"\r\n\r\n")
                connects.append(header.split(b"\r\n", 1)[0])
                reply = b"HTTP/1.1 200 Connection established\r\n\r\n"
            peer_read, peer_write = await asyncio.open_connection("127.0.0.1", tls_port)
            writer.write(reply)
            await writer.drain()
            await asyncio.gather(relay(reader, peer_write), relay(peer_read, writer))

        proxy_server = await asyncio.start_server(
            proxy, "127.0.0.1", 0, ssl=context if proxy_scheme == "https" else None,
        )
        proxy_port = proxy_server.sockets[0].getsockname()[1]
        proxy_url = f"{proxy_scheme}://127.0.0.1:{proxy_port}"
        monkeypatch.setenv("HTTPS_PROXY", proxy_url)
        options = (
            {"trusted_proxy_urls": [proxy_url]} if proxy_dns is True
            else {"trusted_proxy_urls": []} if proxy_dns is False else {}
        )
        if proxy_dns == "bypass":
            monkeypatch.setenv("NO_PROXY", "example.test")
        provider = LocalExtractProvider(node("local_extract", options))

        loop = asyncio.get_running_loop()
        lookup = loop.getaddrinfo

        async def resolve(host, port, **kwargs):
            name = host.decode("ascii") if isinstance(host, bytes) else host
            if name == "example.test":
                if proxy_dns != "bypass":
                    raise socket.gaierror("target can only be resolved by proxy")
                return await lookup("127.0.0.1", port, **kwargs)
            return await lookup(host, port, **kwargs)

        monkeypatch.setattr(loop, "getaddrinfo", resolve)
        target_url = (
            f"https://example.test:{tls_port}/article" if proxy_dns == "bypass"
            else "https://example.test/article"
        )
        try:
            async with provider.create_http_client() as client:
                provider._http_client = client
                if hostname == "wrong.test":
                    with pytest.raises(ProviderError, match="network error"):
                        await asyncio.wait_for(provider.extract(target_url), 8)
                    assert not requests
                else:
                    result = await asyncio.wait_for(provider.extract(target_url), 8)
                    assert result.title == "CONNECT article"
                    assert "certificate validation" in result.content
                    host = f"example.test:{tls_port}" if proxy_dns == "bypass" else "example.test"
                    assert f"Host: {host}\r\n".encode() in requests[0]
            expected_sni = (
                [None, "example.test"] if proxy_scheme == "https" and proxy_dns != "bypass"
                else ["example.test"]
            )
            assert sni == expected_sni
            target_host = "example.test"
            assert connects == ([] if proxy_dns == "bypass" else [
                f"CONNECT {target_host}:443 HTTP/1.1".encode(),
            ])
        finally:
            proxy_server.close()
            tls_server.close()
            await proxy_server.wait_closed()
            await tls_server.wait_closed()
    asyncio.run(run())
