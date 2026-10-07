"""故障转移编排：按顺序尝试各节点，限制耗时并提供短时熔断。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from typing import Any

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


class RequestTimeoutError(AllProvidersFailedError):
    """调用总预算耗尽，不代表尚未尝试的节点不可用。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        Exception.__init__(self, "request timeout: " + "; ".join(errors))


class _FailoverRouter:
    """通用故障转移基类：串行尝试 + 耗时预算 + 单探测恢复。"""

    def __init__(
        self,
        providers: Iterable[object],
        breaker_seconds: float = 60.0,
        total_timeout: float | None = None,
    ) -> None:
        self.providers: list = list(providers)
        self.breaker_seconds = max(0.0, float(breaker_seconds))
        self.total_timeout = (
            total_timeout if total_timeout is not None
            else sum(provider.timeout for provider in self.providers)
        )
        self._fail_at: dict[str, float] = {}
        self._probing: set[str] = set()

    def _skip_if_open(self, provider: object, errors: list[str]) -> bool:
        name = provider.name
        if self.breaker_seconds == 0:
            return False
        fail_at = self._fail_at.get(name)
        if name in self._probing:
            reason = "recovery probe in progress"
        elif fail_at is None:
            return False
        elif time.monotonic() - fail_at >= self.breaker_seconds:
            # 检查与占用间不 await，同一事件循环中只放行一个恢复探测。
            self._probing.add(name)
            return False
        else:
            reason = "breaker open"
        logger.warning("node=%s skipped reason=%s", name, reason)
        errors.append(f"[{name}] {reason}")
        return True

    def _record_failure(
        self, provider: object, exc: Exception, errors: list[str], elapsed: float
    ) -> None:
        name = provider.name
        reason = getattr(exc, "reason", None) or repr(exc)
        self._fail_at[name] = time.monotonic()
        logger.warning(
            "node=%s failed elapsed=%.3fs reason=%s -> failover to next",
            name, elapsed, reason,
        )
        errors.append(f"[{name}] {reason}")

    def _budget_error(self, errors: list[str], index: int) -> RequestTimeoutError:
        for provider in self.providers[index:]:
            logger.warning("node=%s skipped reason=total budget exhausted", provider.name)
        return RequestTimeoutError([*errors, "total budget exhausted"])

    async def _route(self, kind: str, args: tuple) -> tuple[Any, str]:
        start = time.monotonic()
        deadline = start + self.total_timeout
        attempted = skipped = 0
        status = "failed"
        errors: list[str] = []
        empty_provider: str | None = None
        try:
            if not self.providers:
                raise AllProvidersFailedError(["no enabled provider"])
            for index, provider in enumerate(self.providers):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    skipped += len(self.providers) - index
                    status = "timeout"
                    raise self._budget_error(errors, index)
                if self._skip_if_open(provider, errors):
                    skipped += 1
                    continue
                is_probe = provider.name in self._probing
                attempted += 1
                # 首节点从整次调用开始计时，使默认单节点预算与节点截止点一致。
                node_start = start if index == 0 else time.monotonic()
                node_deadline = node_start + provider.timeout
                node_limited = node_deadline <= deadline
                timeout = max(0.0, min(node_deadline, deadline) - time.monotonic())
                try:
                    result = await asyncio.wait_for(
                        getattr(provider, kind)(*args), timeout=timeout
                    )
                except asyncio.TimeoutError as exc:
                    elapsed = time.monotonic() - node_start
                    budget_exhausted = time.monotonic() >= deadline
                    # 先按实际限制来源记录节点故障，不能被回调调度延迟改成预算截断。
                    if node_limited or not budget_exhausted:
                        self._record_failure(
                            provider,
                            ProviderError(provider.name, f"node timeout after {provider.timeout:g}s"),
                            errors, elapsed,
                        )
                    if budget_exhausted:
                        if not node_limited:
                            logger.warning(
                                "node=%s stopped elapsed=%.3fs reason=total budget exhausted",
                                provider.name, elapsed,
                            )
                        skipped += len(self.providers) - index - 1
                        status = "timeout"
                        raise self._budget_error(errors, index + 1) from exc
                    continue
                except Exception as exc:  # noqa: BLE001 - 防御性兜底
                    self._record_failure(
                        provider, exc, errors, time.monotonic() - node_start
                    )
                    continue
                finally:
                    # 取消或预算截止也必须释放探测占用，避免阻塞后续恢复。
                    if is_probe:
                        self._probing.discard(provider.name)
                elapsed = time.monotonic() - node_start
                if time.monotonic() >= deadline:
                    skipped += len(self.providers) - index - 1
                    status = "timeout"
                    raise self._budget_error(errors, index + 1)
                self._fail_at.pop(provider.name, None)
                if kind == "search" and not result:
                    empty_provider = provider.name
                    logger.info(
                        "node=%s empty results elapsed=%.3fs -> failover to next",
                        provider.name, elapsed,
                    )
                    continue
                extra = (
                    f"results={len(result)} query={args[0][:80]!r}"
                    if kind == "search"
                    else f"content_len={len(result.content)} url={args[0][:120]!r}"
                )
                logger.info("node=%s ok elapsed=%.3fs %s", provider.name, elapsed, extra)
                status = "ok"
                return result, provider.name
            if empty_provider is not None:
                status = "empty"
                return [], empty_provider
            logger.error("all providers failed kind=%s errors=%s", kind, errors)
            raise AllProvidersFailedError(errors)
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            logger.info(
                "request kind=%s status=%s elapsed=%.3fs attempted=%d skipped=%d",
                kind, status, time.monotonic() - start, attempted, skipped,
            )


class SearchRouter(_FailoverRouter):
    """搜索故障转移路由，空结果继续切换但不触发熔断。"""

    def __init__(
        self,
        providers: Iterable[SearchProvider],
        breaker_seconds: float = 60.0,
        total_timeout: float | None = None,
    ) -> None:
        super().__init__(providers, breaker_seconds, total_timeout)

    async def search(
        self, query: str, max_results: int = 5
    ) -> tuple[list[SearchResult], str]:
        """返回 (结果, 命中节点名)；真实失败或预算耗尽时抛异常。"""
        return await self._route("search", (query, max_results))


class ExtractRouter(_FailoverRouter):
    """网页抓取故障转移路由。"""

    def __init__(
        self,
        providers: Iterable[ExtractProvider],
        breaker_seconds: float = 60.0,
        total_timeout: float | None = None,
    ) -> None:
        super().__init__(providers, breaker_seconds, total_timeout)

    async def extract(self, url: str) -> tuple[ExtractResult, str]:
        """返回 (结果, 命中节点名)；真实失败或预算耗尽时抛异常。"""
        return await self._route("extract", (url,))
