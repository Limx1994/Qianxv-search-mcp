"""AnySearch 搜索适配器。

端点: POST https://api.anysearch.com/v1/search
鉴权: Authorization: Bearer <api_key>（可选，匿名亦可用）
响应: code==0 时取 data.results[] 的 title/url/snippet/content
"""

from __future__ import annotations

from .base import ProviderError, SearchProvider, SearchResult


class AnySearchProvider(SearchProvider):
    async def search(
        self, query: str, max_results: int
    ) -> list[SearchResult]:
        if not self.endpoint:
            raise ProviderError(self.name, "endpoint not configured")
        body: dict[str, object] = {
            "query": query,
            "max_results": min(max(1, max_results), 10),
            "format": "json",
        }
        for key in ("zone", "language", "tag"):
            if key in self.options:
                body[key] = self.options[key]

        data = await self._post_json(self.endpoint, body)
        if data.get("code", 0) != 0:
            raise ProviderError(
                self.name,
                f"biz error code={data.get('code')} "
                f"message={str(data.get('message'))[:200]}",
            )
        results_raw = (data.get("data") or {}).get("results") or []
        results = [
            SearchResult(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                snippet=str(
                    item.get("snippet") or item.get("content") or ""
                )[:500],
                source=self.name,
            )
            for item in results_raw
            if isinstance(item, dict)
        ]
        return results[:max_results]
