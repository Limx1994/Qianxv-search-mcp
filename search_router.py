"""故障转移编排：按顺序尝试各节点，失败自动切换（搜索/抓取共用）。"""

from __future__ import annotations

import time
from collections.abc import Iterable

from logger import setup_logging
from providers.base import ProviderError, SearchProvider, SearchResult
from providers.extract_base import ExtractProvider, ExtractResult

logger = setup_logging()


class AllProvidersFailedError(Exception):
    """全部节点均失败。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        detail = "; ".join(errors) if errors else "no enabled node"
        super().__init__(f"all providers failed: {detail}")


class _FailoverRouter:
    """通用故障转移基类：串行尝试 + 短时熔断 + 日志。"""

    def __init__(
        self,
        providers: Iterable[object],
        breaker_seconds: float = 60.0,
    ) -> None:
        self.providers: list = list(providers)
        self.breaker_seconds = max(0.0, float(breaker_seconds))
        self._fail_at: dict[str, float] = {}

    def _breaker_open(self, name: str) -> bool:
        fail_at = self._fail_at.get(name)
        if fail_at is None:
            return False
        if time.monotonic() - fail_at >= self.breaker_seconds:
            self._fail_at.pop(name, None)
            return False
        return True

    def _skip_if_open(
        self, provider: object, errors: list[str]
    ) -> bool:
        """熔断打开时跳过节点并记错误；返回 True 表示已跳过。"""
        if not self._breaker_open(getattr(provider, "name", "")):
            return False
        name = getattr(provider, "name", "")
        remaining = int(
            self.breaker_seconds
            - (time.monotonic() - self._fail_at[name])
        )
        logger.warning(
            "node=%s skipped (breaker open, %ss left)",
            name,
            max(remaining, 0),
        )
        errors.append(f"[{name}] breaker open")
        return True

    def _record_failure(
        self, provider: object, exc: Exception, errors: list[str]
    ) -> None:
        name = getattr(provider, "name", "")
        reason = getattr(exc, "reason", None) or repr(exc)
        self._fail_at[name] = time.monotonic()
        logger.warning(
            "node=%s failed reason=%s -> failover to next",
            name,
            reason,
        )
        errors.append(f"[{name}] {reason}")

    def _record_empty(self, provider: object, errors: list[str]) -> None:
        name = getattr(provider, "name", "")
        self._fail_at[name] = time.monotonic()
        logger.warning(
            "node=%s empty results -> failover to next", name
        )
        errors.append(f"[{name}] empty results")

    def _log_all_failed(self, label: str, errors: list[str]) -> None:
        logger.error("all providers failed kind=%s errors=%s", label, errors)

    def _log_success(
        self, provider: object, elapsed: float, extra: str
    ) -> None:
        name = getattr(provider, "name", "")
        logger.info(
            "node=%s ok elapsed=%.1fs %s", name, elapsed, extra
        )


class SearchRouter(_FailoverRouter):
    """搜索故障转移路由（行为与旧版一致）。"""

    def __init__(
        self,
        providers: Iterable[SearchProvider],
        breaker_seconds: float = 60.0,
    ) -> None:
        super().__init__(providers, breaker_seconds)

    async def search(
        self, query: str, max_results: int = 5
    ) -> tuple[list[SearchResult], str]:
        """返回 (结果, 命中节点名)；全部失败抛 AllProvidersFailedError。"""
        if not self.providers:
            raise AllProvidersFailedError(["no enabled provider"])
        errors: list[str] = []
        for provider in self.providers:
            if self._skip_if_open(provider, errors):
                continue
            start = time.monotonic()
            try:
                results = await provider.search(query, max_results)
            except ProviderError as exc:
                self._record_failure(provider, exc, errors)
                continue
            except Exception as exc:  # noqa: BLE001 - 防御性兜底
                self._record_failure(provider, exc, errors)
                continue
            elapsed = time.monotonic() - start
            if not results:
                self._record_empty(provider, errors)
                continue
            self._fail_at.pop(provider.name, None)
            self._log_success(
                provider,
                elapsed,
                f"results={len(results)} query={query[:80]!r}",
            )
            return results, provider.name
        self._log_all_failed("search", errors)
        raise AllProvidersFailedError(errors)


class ExtractRouter(_FailoverRouter):
    """网页抓取故障转移路由。"""

    def __init__(
        self,
        providers: Iterable[ExtractProvider],
        breaker_seconds: float = 60.0,
    ) -> None:
        super().__init__(providers, breaker_seconds)

    async def extract(self, url: str) -> tuple[ExtractResult, str]:
        """返回 (结果, 命中节点名)；全部失败抛 AllProvidersFailedError。"""
        if not self.providers:
            raise AllProvidersFailedError(["no enabled provider"])
        errors: list[str] = []
        for provider in self.providers:
            if self._skip_if_open(provider, errors):
                continue
            start = time.monotonic()
            try:
                result = await provider.extract(url)
            except ProviderError as exc:
                self._record_failure(provider, exc, errors)
                continue
            except Exception as exc:  # noqa: BLE001 - 防御性兜底
                self._record_failure(provider, exc, errors)
                continue
            elapsed = time.monotonic() - start
            self._fail_at.pop(provider.name, None)
            self._log_success(
                provider,
                elapsed,
                f"content_len={len(result.content)} url={url[:120]!r}",
            )
            return result, provider.name
        self._log_all_failed("extract", errors)
        raise AllProvidersFailedError(errors)
