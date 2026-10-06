"""搜索节点抽象基类与公共 HTTP 辅助。"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("mcp_search")


@dataclass
class SearchResult:
    """标准化搜索结果。"""

    title: str
    url: str
    snippet: str
    source: str


class ProviderError(Exception):
    """单个搜索节点失败（网络/超时/HTTP错误/鉴权/配额等）。"""

    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(f"[{provider}] {reason}")
        self.provider = provider
        self.reason = reason


class SearchProvider(ABC):
    """搜索节点抽象基类；子类实现 search() 并解析为 SearchResult 列表。"""

    def __init__(self, node_cfg: Any) -> None:
        self.cfg = node_cfg
        self.name: str = node_cfg.name
        self.api_key: str = getattr(node_cfg, "api_key", "")
        self.timeout: float = float(
            getattr(node_cfg, "timeout_seconds", 10.0)
        )
        self.options: dict = dict(getattr(node_cfg, "options", {}))
        self._http_client: httpx.AsyncClient | None = None

    @abstractmethod
    async def search(
        self, query: str, max_results: int
    ) -> list[SearchResult]:
        """执行搜索；任何失败必须抛出 ProviderError。"""

    @property
    def endpoint(self) -> str:
        return str(self.options.get("endpoint", ""))

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _post_json(
        self, url: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """统一 POST 请求：超时/网络/非2xx/解析失败均转 ProviderError。"""
        return await self._request_json("POST", url, body=body)

    async def _get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """统一 GET 请求：超时/网络/非2xx/解析失败均转 ProviderError。"""
        return await self._request_json(
            "GET", url, params=params, extra_headers=headers
        )

    async def _request_json(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """统一 HTTP 请求：超时/网络/非2xx/解析失败均转 ProviderError。"""
        headers = self._auth_headers()
        if extra_headers:
            headers.update(extra_headers)
        if self._http_client is not None:
            # 注入的共享客户端由服务生命周期关闭，请求结束时只退出空上下文。
            client_context = nullcontext(self._http_client)
        else:
            # 单独调用适配器时创建临时客户端，由下方 async with 负责关闭。
            logger.warning(
                "Provider %s: _http_client not injected, creating new client per request. "
                "This may cause performance issues. Ensure lifespan is properly initialized.",
                self.name
            )
            client_context = httpx.AsyncClient(timeout=self.timeout)
        try:
            async with client_context as client:
                if method == "GET":
                    resp = await client.get(
                        url, headers=headers, params=params, timeout=self.timeout
                    )
                else:
                    resp = await client.post(
                        url, headers=headers, json=body, timeout=self.timeout
                    )
        except httpx.TimeoutException as exc:
            raise ProviderError(self.name, f"timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"network error: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(
                self.name,
                f"HTTP {resp.status_code}: {resp.text[:200]}",
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(
                self.name, "invalid JSON response"
            ) from exc
        if not isinstance(data, dict):
            raise ProviderError(self.name, "unexpected response shape")
        return data
