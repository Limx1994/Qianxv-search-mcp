"""知乎全网搜索适配器。

端点: GET https://developer.zhihu.com/api/v1/content/global_search
鉴权: Authorization: Bearer <Access Secret> + X-Request-Timestamp(秒级)
响应: Data.Items[] 的 Title/Url/ContentText
"""

from __future__ import annotations

import re
import time

from .base import ProviderError, SearchProvider, SearchResult

_EM_TAG_RE = re.compile(r"</?em>")


class ZhihuProvider(SearchProvider):
    async def search(
        self, query: str, max_results: int
    ) -> list[SearchResult]:
        if not self.endpoint:
            raise ProviderError(self.name, "endpoint not configured")
        if not self.api_key:
            raise ProviderError(self.name, "api_key required")
        params: dict[str, object] = {
            "Query": query,
            "Count": min(max(1, max_results), 20),
        }
        headers = {"X-Request-Timestamp": str(int(time.time()))}

        data = await self._get_json(self.endpoint, params, headers=headers)
        code = data.get("Code", 0)
        if code != 0:
            raise ProviderError(
                self.name,
                f"biz error: code={code} {str(data.get('Message', ''))[:200]}",
            )
        data_obj = data.get("Data")
        items = data_obj.get("Items") if isinstance(data_obj, dict) else None
        results_raw = items or []
        results = [
            SearchResult(
                title=str(item.get("Title", "")),
                url=str(item.get("Url", "")),
                snippet=_EM_TAG_RE.sub(
                    "", str(item.get("ContentText") or "")
                )[:500],
                source=self.name,
            )
            for item in results_raw
            if isinstance(item, dict)
        ]
        return results[:max_results]
