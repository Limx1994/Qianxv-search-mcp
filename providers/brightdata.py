"""Bright Data 搜索适配器（官方 MCP 端点）。

端点: POST https://mcp.brightdata.com/mcp (MCP Streamable HTTP)
鉴权: Authorization: Bearer <api_key>
工具: search_engine {"query": ..., "engine": "google"}
响应: content[0].text 中 UNTRUSTED 标记包裹的 JSON，organic[] 字段
      title/link/description

注意：账号需在 Bright Data 控制台激活爬虫产品，否则返回空 organic。
"""

from __future__ import annotations

import json
import re
from contextlib import nullcontext
from typing import Any

import httpx

from .base import ProviderError, SearchProvider, SearchResult

_UNTRUSTED_RE = re.compile(
    r"=====UNTRUSTED_[0-9a-f]+_BEGIN=====\n(.*?)\n=====UNTRUSTED_[0-9a-f]+_END=====",
    re.DOTALL,
)


class BrightDataProvider(SearchProvider):
    """通过 Bright Data 官方 MCP 的 search_engine 工具搜索。"""

    async def search(
        self, query: str, max_results: int
    ) -> list[SearchResult]:
        if not self.endpoint:
            raise ProviderError(self.name, "endpoint not configured")
        if not self.api_key:
            raise ProviderError(self.name, "api_key required")
        engine = str(self.options.get("engine", "google"))
        geo = str(self.options.get("geo_location", "")).strip()

        args: dict[str, Any] = {"query": query, "engine": engine}
        if geo:
            args["geo_location"] = geo
        text = await self._call_tool("search_engine", args)
        data = self._parse_payload(text)
        results_raw = data.get("organic") or []
        results = [
            SearchResult(
                title=str(item.get("title", "")),
                url=str(item.get("link") or item.get("url") or ""),
                snippet=str(
                    item.get("description") or item.get("snippet") or ""
                )[:500],
                source=self.name,
            )
            for item in results_raw
            if isinstance(item, dict)
        ]
        return results[:max_results]

    async def _call_tool(self, tool: str, arguments: dict[str, Any]) -> str:
        """完整 MCP 会话：initialize -> tools/call，返回 content 文本。"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        client_context = (
            nullcontext(self._http_client)
            if self._http_client is not None
            else httpx.AsyncClient(timeout=self.timeout)
        )
        try:
            async with client_context as client:
                resp = await client.post(
                    self.endpoint,
                    headers=headers,
                    timeout=self.timeout,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {
                                "name": "search-mcp",
                                "version": "1.0",
                            },
                        },
                    },
                )
                session_id = resp.headers.get("mcp-session-id", "")
                if session_id:
                    headers["mcp-session-id"] = session_id
                await client.post(
                    self.endpoint,
                    headers=headers,
                    timeout=self.timeout,
                    json={
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                    },
                )
                resp = await client.post(
                    self.endpoint,
                    headers=headers,
                    timeout=self.timeout,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": arguments},
                    },
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
        payload = self._parse_sse(resp.text)
        if payload is None:
            raise ProviderError(self.name, "invalid MCP response")
        if payload.get("error"):
            raise ProviderError(
                self.name,
                f"MCP error: {str(payload['error'])[:200]}",
            )
        content = (payload.get("result") or {}).get("content") or []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return str(block.get("text", ""))
        raise ProviderError(self.name, "no text content in MCP response")

    @staticmethod
    def _parse_sse(text: str) -> dict[str, Any] | None:
        """从 SSE 流提取最后一条 data: JSON（JSON-RPC 响应）。"""
        payload: dict[str, Any] | None = None
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    data = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(data, dict):
                    payload = data
        return payload

    def _parse_payload(self, text: str) -> dict[str, Any]:
        """提取 UNTRUSTED 标记内的 JSON 并解析为 dict。"""
        match = _UNTRUSTED_RE.search(text)
        if match is None:
            raise ProviderError(
                self.name, "unexpected search_engine payload"
            )
        try:
            data = json.loads(match.group(1))
        except ValueError as exc:
            raise ProviderError(
                self.name, "invalid JSON in search_engine payload"
            ) from exc
        if not isinstance(data, dict):
            raise ProviderError(self.name, "unexpected payload shape")
        return data
