"""本机网页抓取：下载公开 HTML 并在本机提取 Markdown 正文。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from contextlib import nullcontext
from urllib.parse import urlsplit

import httpx
from trafilatura import extract as extract_html
from trafilatura.metadata import extract_metadata

from .base import ProviderError
from .extract_base import ExtractProvider, ExtractResult, logger

_MAX_BYTES = 5 * 1024 * 1024
_MAX_REDIRECTS = 5


class LocalExtractProvider(ExtractProvider):
    def create_http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout)

    async def extract(self, url: str) -> ExtractResult:
        if self._http_client is not None:
            client_context = nullcontext(self._http_client)
        else:
            logger.warning(
                "Extract provider %s: _http_client not injected, "
                "creating new client per request.", self.name,
            )
            client_context = self.create_http_client()
        try:
            async with client_context as client:
                data, final_url = await self._download(client, url)
            return await asyncio.to_thread(self._parse_html, data, final_url)
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError(self.name, "download timeout") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                self.name, f"network error ({type(exc).__name__})"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 统一报告解析及第三方库失败
            raise ProviderError(
                self.name, f"HTML extraction failed ({type(exc).__name__})"
            ) from exc

    async def _public_target(self, url: str) -> httpx.URL:
        try:
            urlsplit(url).port
            parsed = httpx.URL(url)
            if (
                parsed.scheme not in ("http", "https") or not parsed.host
                or parsed.username or parsed.password
                or any(char.isspace() or ord(char) < 32 for char in url)
            ):
                raise ValueError("invalid URL")
            host = parsed.raw_host.decode("ascii")
        except (httpx.InvalidURL, ValueError) as exc:
            raise ProviderError(self.name, "invalid public HTTP/HTTPS URL") from exc
        host_name = host.lower().rstrip(".")
        if host_name == "localhost" or host_name.endswith(".localhost"):
            raise ProviderError(self.name, "non-public address blocked")
        try:
            address = ipaddress.ip_address(host_name)
        except ValueError:
            try:
                address = ipaddress.ip_address(socket.inet_aton(host_name))
            except OSError:
                # 域名解析与代理选择交给 HTTP 客户端和运行环境。
                return parsed.copy_with(fragment=None)
        if not address.is_global or address.is_multicast or address.is_unspecified:
            raise ProviderError(self.name, "non-public address blocked")
        return parsed.copy_with(fragment=None)

    async def _download(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[bytes, str]:
        for index in range(_MAX_REDIRECTS + 1):
            original = await self._public_target(url)
            # 保留原始域名，由 HTTP 客户端处理 DNS、代理及 TLS。
            # 每一跳独立构建请求，不向目标发送 cookies 或鉴权。
            request = httpx.Request(
                "GET", original,
                headers={
                    "User-Agent": "Qianxv-search-mcp/local-extract",
                    "Accept": "text/html, application/xhtml+xml",
                    "Connection": "close",
                },
                extensions={
                    "timeout": httpx.Timeout(self.timeout).as_dict(),
                },
            )
            response = await client.send(
                request, stream=True, follow_redirects=False, auth=None,
            )
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise ProviderError(self.name, "redirect without Location")
                    if index == _MAX_REDIRECTS:
                        continue
                    url = str(original.join(location))
                    continue
                if not 200 <= response.status_code < 300:
                    raise ProviderError(self.name, f"HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "")
                mime = content_type.split(";", 1)[0].strip().lower()
                if mime not in ("text/html", "application/xhtml+xml"):
                    raise ProviderError(self.name, "response is not HTML")
                length = response.headers.get("content-length")
                if length is not None:
                    try:
                        size = int(length)
                        if size < 0:
                            raise ValueError("negative Content-Length")
                    except ValueError as exc:
                        raise ProviderError(self.name, "invalid Content-Length") from exc
                    if size > _MAX_BYTES:
                        raise ProviderError(self.name, "download exceeds 5 MiB limit")
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    if len(data) + len(chunk) > _MAX_BYTES:
                        raise ProviderError(self.name, "download exceeds 5 MiB limit")
                    data.extend(chunk)
                return bytes(data), str(original)
            finally:
                await response.aclose()
        raise ProviderError(self.name, "too many redirects")

    def _parse_html(self, data: bytes, url: str) -> ExtractResult:
        content = extract_html(
            data, url=url, output_format="markdown",
            include_comments=False, include_links=True,
        )
        if not content or not content.strip():
            raise ProviderError(self.name, "empty extracted content")
        metadata = extract_metadata(data, default_url=url)
        return ExtractResult(
            title=metadata.title or "" if metadata is not None else "",
            url=url, content=content, source=self.name,
        )
