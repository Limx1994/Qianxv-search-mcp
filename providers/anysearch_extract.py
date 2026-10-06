"""AnySearch 网页抓取适配器。

端点: POST https://api.anysearch.com/v1/extract
鉴权: Authorization: Bearer as_sk_...
请求体: 严格仅 {"url": "<绝对URL>"} 一个字段
响应: code==0 时取 data.{url,title,content}
"""

from __future__ import annotations

from .base import ProviderError
from .extract_base import ExtractProvider, ExtractResult


class AnySearchExtractProvider(ExtractProvider):
    async def extract(self, url: str) -> ExtractResult:
        if not self.endpoint:
            raise ProviderError(self.name, "endpoint not configured")
        self._require_key()
        # 请求体严格仅含 url 一个字段，多余字段会被 400 拒绝
        body: dict[str, str] = {"url": url}

        data = await self._post_json(self.endpoint, body)
        if data.get("code", 0) != 0:
            raise ProviderError(
                self.name,
                f"biz error code={data.get('code')} "
                f"message={str(data.get('message'))[:200]}",
            )
        payload = data.get("data") or {}
        content = str(payload.get("content") or "")
        # 空正文作为节点失败上报，由抓取路由继续尝试其他节点。
        if not content:
            raise ProviderError(self.name, "empty extracted content")
        return ExtractResult(
            title=str(payload.get("title") or ""),
            url=str(payload.get("url") or url),
            content=content,
            source=self.name,
        )
