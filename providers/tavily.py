"""Tavily 搜索适配器。

端点: POST https://api.tavily.com/search
鉴权: Authorization: Bearer tvly-...
响应: results[] 的 title/url/content
"""

from __future__ import annotations

from .base import ProviderError, SearchProvider, SearchResult


class TavilyProvider(SearchProvider):
    async def search(
        self, query: str, max_results: int
    ) -> list[SearchResult]:
        if not self.endpoint:
            raise ProviderError(self.name, "endpoint not configured")
        if not self.api_key:
            raise ProviderError(self.name, "api_key required")
        body: dict[str, object] = {
            "query": query,
            "max_results": min(max(1, max_results), 20),
            "search_depth": "basic",
        }

        data = await self._post_json(self.endpoint, body)
        if data.get("detail"):
            raise ProviderError(
                self.name, f"biz error: {str(data['detail'])[:200]}"
            )
        results_raw = data.get("results") or []
        results = [
            SearchResult(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                snippet=str(item.get("content") or "")[:500],
                source=self.name,
            )
            for item in results_raw
            if isinstance(item, dict)
        ]
        return results[:max_results]
