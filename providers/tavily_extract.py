"""Tavily 网页抓取适配器。

端点: POST https://api.tavily.com/extract
鉴权: Authorization: Bearer tvly-...
请求体: {"urls": "<url>", "format": "markdown"}
响应: results[].{url, raw_content}；HTTP 200 也可能 results 为空
（需按失败处理触发故障转移）。
"""

from __future__ import annotations

from .base import ProviderError
from .extract_base import ExtractProvider, ExtractResult


class TavilyExtractProvider(ExtractProvider):
    async def extract(self, url: str) -> ExtractResult:
        if not self.endpoint:
            raise ProviderError(self.name, "endpoint not configured")
        self._require_key()
        body: dict[str, object] = {"urls": url, "format": "markdown"}

        data = await self._post_json(self.endpoint, body)
        if data.get("detail"):
            raise ProviderError(
                self.name, f"biz error: {str(data['detail'])[:200]}"
            )
        results = data.get("results") or []
        # 注意：Tavily 提取失败也可能返回 HTTP 200 + 空 results
        if not results:
            failed = data.get("failed_results") or []
            reason = "empty results"
            if failed and isinstance(failed[0], dict):
                reason = f"extract failed: {str(failed[0].get('error'))[:150]}"
            raise ProviderError(self.name, reason)
        item = results[0]
        if not isinstance(item, dict):
            raise ProviderError(self.name, "unexpected result shape")
        content = str(item.get("raw_content") or "")
        if not content:
            raise ProviderError(self.name, "empty extracted content")
        # 从正文首个非空一级标题提取标题；没有标题时保留空字符串。
        title = next(
            (line[2:].strip() for line in content.splitlines()
             if line.startswith("# ") and line[2:].strip()),
            "",
        )
        return ExtractResult(
            title=title,
            url=str(item.get("url") or url),
            content=content,
            source=self.name,
        )
