"""百度千帆 AI 搜索适配器。

端点: POST https://qianfan.baidubce.com/v2/ai_search/web_search
鉴权: Authorization: Bearer bce-v3/...
响应: references[] 的 title/url/content|snippet
"""

from __future__ import annotations

import logging

from .base import ProviderError, SearchProvider, SearchResult

logger = logging.getLogger("mcp_search")


class QianfanProvider(SearchProvider):
    async def search(
        self, query: str, max_results: int
    ) -> list[SearchResult]:
        if not self.endpoint:
            raise ProviderError(self.name, "endpoint not configured")
        # 百度千帆 API 限制：query 最大 144 字符
        truncated_query = query[:144]
        if len(query) > 144:
            logger.warning(
                "Provider %s: query truncated from %d to 144 characters (API limit)",
                self.name, len(query)
            )
        body: dict[str, object] = {
            "messages": [{"role": "user", "content": truncated_query}],
            "search_source": self.options.get(
                "search_source", "baidu_search_v2"
            ),
            "resource_type_filter": [
                {"type": "web", "top_k": min(max(1, max_results), 50)}
            ],
        }
        if not self.api_key:
            raise ProviderError(self.name, "api_key required")

        data = await self._post_json(self.endpoint, body)
        if data.get("code") or data.get("error_code"):
            raise ProviderError(
                self.name,
                f"biz error code={data.get('code') or data.get('error_code')} "
                f"message={str(data.get('message') or data.get('error_message'))[:200]}",
            )
        references = data.get("references") or []
        results = [
            SearchResult(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                snippet=str(
                    item.get("snippet") or item.get("content") or ""
                )[:500],
                source=self.name,
            )
            for item in references
            if isinstance(item, dict)
        ]
        return results[:max_results]
