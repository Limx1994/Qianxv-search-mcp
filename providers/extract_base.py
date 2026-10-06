"""网页抓取（内容提取）节点抽象基类。

HTTP 失败判定逻辑与 base.py 的 SearchProvider 保持一致
（超时/网络/非2xx/JSON解析统一转 ProviderError）。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

import httpx

from .base import ProviderError

logger = logging.getLogger("mcp_search")


@dataclass
class ExtractResult:
    """标准化网页抓取结果。"""

    title: str
    url: str
    content: str
    source: str


class ExtractProvider(ABC):
    """抓取节点抽象基类；子类实现 extract() 并解析为 ExtractResult。"""

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
    async def extract(self, url: str) -> ExtractResult:
        """抓取公开网页内容；任何失败必须抛出 ProviderError。"""

    @property
    def endpoint(self) -> str:
        return str(self.options.get("endpoint", ""))

    def _require_key(self) -> None:
        if not self.api_key:
            raise ProviderError(self.name, "api_key required")

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _post_json(
        self, url: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """统一 POST 请求：超时/网络/非2xx/解析失败均转 ProviderError。"""
        if self._http_client is not None:
            # 共享客户端的关闭责任属于服务生命周期，不能在单次抓取后关闭。
            client_context = nullcontext(self._http_client)
        else:
            # 未注入客户端时使用临时连接，成功或异常退出都会由上下文关闭。
            logger.warning(
                "Extract provider %s: _http_client not injected, creating new client per request. "
                "This may cause performance issues. Ensure lifespan is properly initialized.",
                self.name
            )
            client_context = httpx.AsyncClient(timeout=self.timeout)
        try:
            async with client_context as client:
                resp = await client.post(
                    url, headers=self._auth_headers(), json=body,
                    timeout=self.timeout,
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
