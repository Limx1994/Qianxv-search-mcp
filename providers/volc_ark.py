"""火山引擎豆包搜索（联网搜索 Custom 版）适配器。

端点: POST https://open.feedcoopapi.com/search_api/web_search
鉴权: Authorization: Bearer <API_KEY>（联网搜索控制台 - API Key 管理）
请求: Query / SearchType=web / Count / Filter.NeedUrl / QueryControl
响应: Result.WebResults[] 的 Title / Url / Snippet / Summary
免费额度: 每账号每月 500 次
"""

from __future__ import annotations

from typing import Any

from .base import ProviderError, SearchProvider, SearchResult


class VolcArkProvider(SearchProvider):
    async def search(
        self, query: str, max_results: int
    ) -> list[SearchResult]:
        if not self.endpoint:
            raise ProviderError(self.name, "endpoint not configured")
        if not self.api_key:
            raise ProviderError(self.name, "api_key required")
        body: dict[str, Any] = {
            "Query": query[:100],
            "SearchType": "web",
            "Count": min(max(1, max_results), 50),
            "Filter": {
                "NeedContent": False,
                "NeedUrl": True,
            },
            "QueryControl": {"QueryRewrite": False},
        }

        data = await self._post_json(self.endpoint, body)
        err = (data.get("ResponseMetadata") or {}).get("Error")
        if err:
            raise ProviderError(
                self.name,
                f"biz error code={err.get('Code')} "
                f"message={str(err.get('Message'))[:200]}",
            )
        if data.get("Result") is None:
            raise ProviderError(self.name, "Result is null in response")
        web_results = (data.get("Result") or {}).get("WebResults") or []
        results = [
            SearchResult(
                title=str(item.get("Title", "")),
                url=str(item.get("Url", "")),
                snippet=str(
                    item.get("Snippet") or item.get("Summary") or ""
                )[:500],
                source=self.name,
            )
            for item in web_results
            if isinstance(item, dict) and item.get("Url")
        ]
        return results[:max_results]
