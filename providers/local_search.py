"""本机 DDGS 搜索适配器：直接访问搜索引擎，无需 API key。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from contextvars import copy_context
from urllib.parse import urlsplit

import httpx
from ddgs import DDGS
from ddgs.engines import ENGINES
from ddgs.engines.bing import Bing

from .base import ProviderError, SearchProvider, SearchResult, logger


# 慢同步引擎使用进程共享的有界线程池，不占用 Bing 解析和 DNS 的默认池。
_SEARCH_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ddgs")


class LocalSearchProvider(SearchProvider):
    async def search(
        self, query: str, max_results: int
    ) -> list[SearchResult]:
        if not query.strip() or max_results < 1:
            raise ProviderError(self.name, "invalid query or max_results")
        backend = self.options.get("backend", "auto")
        region = self.options.get("region", "cn-zh")
        available = {
            name for name, engine in ENGINES.get("text", {}).items()
            if not getattr(engine, "disabled", False)
        }
        if (
            not isinstance(backend, str)
            or not backend.strip()
            or (backend not in ("auto", "all") and any(
                name.strip() not in available | {"bing_html"}
                for name in backend.split(",")
            ))
        ):
            raise ProviderError(self.name, "unsupported or disabled text backend")
        if not isinstance(region, str) or not region.strip():
            raise ProviderError(self.name, "region must be a non-empty string")
        backends = (
            [*sorted(available), "bing_html"] if backend in ("auto", "all")
            else list(dict.fromkeys(name.strip() for name in backend.split(",")))
        )
        if len(backends) > 1:
            try:
                return await asyncio.wait_for(
                    self._search_many(query, max_results, backends, region), self.timeout,
                )
            except asyncio.TimeoutError as exc:
                raise ProviderError(self.name, "local search timeout") from exc
        return await self._search_backend(query, max_results, backends[0], region)

    async def _search_backend(
        self, query: str, max_results: int, backend: str, region: str,
    ) -> list[SearchResult]:
        try:
            if backend == "bing_html":
                return await self._bing_search(query, max_results, region)
            context = copy_context()
            return await asyncio.get_running_loop().run_in_executor(
                _SEARCH_POOL, context.run,
                self._search_sync, query, max_results, backend, region,
            )
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一报告第三方库失败
            # 第三方异常可能包含带凭证的代理 URL，不将原文写入日志。
            raise ProviderError(
                self.name, f"DDGS search failed ({type(exc).__name__})"
            ) from exc

    async def _search_many(
        self, query: str, max_results: int, backends: list[str], region: str,
    ) -> list[SearchResult]:
        # 单独指定每个后端，避免 DDGS 内部等待慢引擎拖延已成功的结果。
        # 所有引擎共享节点预算；返回首个非空结果，不持久化机器可用性白名单。
        async def attempt(backend: str) -> tuple[str, list[SearchResult]]:
            try:
                rows = await self._search_backend(query, max_results, backend, region)
                if not rows:
                    raise ProviderError(self.name, "empty search results")
                return backend, rows
            except ProviderError as exc:
                raise ProviderError(self.name, f"{backend}: {exc.reason}") from exc

        tasks = [asyncio.create_task(attempt(backend)) for backend in backends]
        errors = []
        try:
            for task in asyncio.as_completed(tasks):
                try:
                    backend, rows = await task
                except ProviderError as exc:
                    errors.append(exc.reason)
                    logger.warning("node=%s engine failed: %s", self.name, exc.reason)
                    continue
                logger.info("node=%s engine=%s selected", self.name, backend)
                return rows
            raise ProviderError(self.name, "all local engines failed: " + "; ".join(errors))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _bing_search(
        self, query: str, max_results: int, region: str
    ) -> list[SearchResult]:
        parts = region.split("-")
        if len(parts) != 2 or not all(part.isalpha() and len(part) == 2 for part in parts):
            raise ProviderError(self.name, "region must use country-language format")
        country, language = parts
        client_context = (
            nullcontext(self._http_client) if self._http_client is not None
            else httpx.AsyncClient(timeout=self.timeout)
        )
        try:
            async with client_context as client:
                target = httpx.URL(Bing.search_url, params={
                    "q": query, "count": min(max_results, 50),
                    "mkt": f"{language}-{country.upper()}",
                })
                for index in range(4):
                    response = await client.get(
                        target, headers={"Accept": "text/html"},
                        timeout=self.timeout, follow_redirects=False, auth=None,
                    )
                    if response.status_code not in (301, 302, 303, 307, 308):
                        break
                    location = response.headers.get("location")
                    if not location or index == 3:
                        raise ProviderError(self.name, "invalid Bing redirect")
                    target = target.join(location)
                    if (
                        target.scheme != "https" or target.port not in (None, 443)
                        or target.host not in ("www.bing.com", "cn.bing.com")
                        or target.username or target.password
                    ):
                        raise ProviderError(self.name, "unsafe Bing redirect")
            if response.status_code != 200:
                raise ProviderError(self.name, f"Bing HTTP {response.status_code}")
            if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/html":
                raise ProviderError(self.name, "Bing response is not HTML")
            return await asyncio.to_thread(self._parse_bing, response.text, max_results)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - 不输出第三方异常中的代理凭证
            raise ProviderError(self.name, f"Bing search failed ({type(exc).__name__})") from exc

    def _parse_bing(self, html: str, max_results: int) -> list[SearchResult]:
        # 复用固定版本 DDGS 的 Bing 解析与跳转 URL 解包，不启用被禁用的原生后端。
        engine = Bing(timeout=self.timeout)
        rows = engine.post_extract_results(engine.extract_results(html))
        if not rows:
            raise ProviderError(self.name, "Bing returned no parsable results")
        results = []
        for row in rows[:max_results]:
            parsed = urlsplit(row.href)
            if not row.title or parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ProviderError(self.name, "invalid Bing search result")
            results.append(SearchResult(row.title, row.href, row.body, self.name))
        return results

    def _search_sync(
        self, query: str, max_results: int, backend: str, region: str
    ) -> list[SearchResult]:
        with DDGS(timeout=self.timeout) as client:
            rows = client.text(
                query, max_results=max_results, backend=backend,
                region=region, safesearch="moderate",
            )
        if not isinstance(rows, list):
            raise ProviderError(self.name, "unexpected search response shape")
        results = []
        for item in rows[:max_results]:
            if not isinstance(item, dict) or any(
                not isinstance(item.get(key), str)
                for key in ("title", "href", "body")
            ):
                raise ProviderError(self.name, "invalid search result fields")
            parsed = urlsplit(item["href"])
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ProviderError(self.name, "invalid search result URL")
            results.append(SearchResult(
                title=item["title"], url=item["href"],
                snippet=item["body"], source=self.name,
            ))
        return results
