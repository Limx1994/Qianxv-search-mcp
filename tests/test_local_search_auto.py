"""跨网络环境的本机搜索引擎选择和预算验证。"""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from config_loader import NodeConfig
from providers import local_search
from providers.base import ProviderError, SearchResult


@pytest.fixture
def engines(monkeypatch):
    class Enabled:
        disabled = False

    class Disabled:
        disabled = True

    monkeypatch.setattr(local_search, "ENGINES", {"text": {
        "duckduckgo": Enabled, "google": Enabled, "bing": Disabled,
    }})


def provider(options=None, timeout=1):
    return local_search.LocalSearchProvider(NodeConfig(
        "local-search", "local_search", True, "", timeout, options or {},
    ))


@pytest.mark.parametrize("winner", ["duckduckgo", "google", "bing_html"])
@pytest.mark.parametrize("backend", [None, "auto", "all"])
def test_auto_different_networks(monkeypatch, engines, winner, backend):
    search = provider({"backend": backend} if backend else {})
    calls, cancelled = [], []

    async def attempt(query, count, name, region):
        calls.append(name)
        assert query == "中文查询" and count == 2 and region == "cn-zh"
        try:
            if name == winner:
                await asyncio.sleep(0.01)
                return [SearchResult("结果", "https://example.test/", "摘要", search.name)]
            await asyncio.sleep(5)
            raise AssertionError("slow engine should have been cancelled")
        except asyncio.CancelledError:
            cancelled.append(name)
            raise

    monkeypatch.setattr(search, "_search_backend", attempt)

    async def run():
        rows = await asyncio.wait_for(search.search("中文查询", 2), 0.3)
        assert len(rows) == 1
        assert set(calls) == {"duckduckgo", "google", "bing_html"}
        assert set(cancelled) == set(calls) - {winner}
        assert len(asyncio.all_tasks()) == 1
    asyncio.run(run())


def test_auto_error_and_empty(monkeypatch, engines):
    search = provider()
    logs = []
    monkeypatch.setattr(local_search.logger, "warning", lambda text, *args: logs.append(text % args))

    async def attempt(query, count, name, region):
        if name == "duckduckgo":
            raise ProviderError(search.name, "network error")
        if name == "google":
            return []
        await asyncio.sleep(0.01)
        return [SearchResult("结果", "https://example.test/", "摘要", search.name)]

    monkeypatch.setattr(search, "_search_backend", attempt)
    assert asyncio.run(search.search("中文", 1))
    assert any("duckduckgo: network error" in log for log in logs)
    assert any("google: empty search results" in log for log in logs)


def test_auto_all_failed(monkeypatch, engines):
    search = provider()

    async def attempt(query, count, name, region):
        raise ProviderError(search.name, "network error")

    monkeypatch.setattr(search, "_search_backend", attempt)
    with pytest.raises(ProviderError, match="all local engines failed") as caught:
        asyncio.run(search.search("中文", 1))
    for name in ("duckduckgo", "google", "bing_html"):
        assert name + ": network error" in caught.value.reason


@pytest.mark.parametrize("cancel", [False, True])
def test_auto_budget_and_cancel(monkeypatch, engines, cancel):
    search = provider(timeout=0.03)
    cancelled = []

    async def attempt(query, count, name, region):
        try:
            await asyncio.sleep(5)
        finally:
            cancelled.append(name)

    monkeypatch.setattr(search, "_search_backend", attempt)

    async def run():
        task = asyncio.create_task(search.search("中文", 1))
        if cancel:
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ProviderError, match="local search timeout"):
                await asyncio.wait_for(task, 0.3)
        assert set(cancelled) == {"duckduckgo", "google", "bing_html"}
        assert len(asyncio.all_tasks()) == 1
    asyncio.run(run())


def test_explicit_engine_list(monkeypatch, engines):
    search = provider({"backend": " google ,bing_html,google "})
    calls = []

    async def attempt(query, count, name, region):
        calls.append(name)
        return [SearchResult("结果", "https://example.test/", "摘要", search.name)]

    monkeypatch.setattr(search, "_search_backend", attempt)
    assert asyncio.run(search.search("中文", 1))
    assert set(calls) == {"google", "bing_html"}
    assert len(calls) == 2


def test_disabled_engine_rejected(engines):
    search = provider({"backend": "bing"})
    with pytest.raises(ProviderError, match="unsupported or disabled"):
        asyncio.run(search.search("中文", 1))


def test_auto_slow_threads_leave_bing_free(monkeypatch, engines):
    release = threading.Event()
    started = threading.Event()
    lock = threading.Lock()
    active = 0
    responses = []

    class SlowDDGS:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def text(self, *args, **kwargs):
            nonlocal active
            with lock:
                active += 1
                if active >= 2:
                    started.set()
            try:
                if not release.wait(5):
                    raise RuntimeError("test did not release slow engines")
                raise RuntimeError("simulated slow engine failure")
            finally:
                with lock:
                    active -= 1

    async def respond(request):
        while not started.is_set():
            await asyncio.sleep(0.001)
        responses.append(request)
        return httpx.Response(200, headers={"content-type": "text/html"}, text=(
            '<li class="b_algo"><h2><a href="https://example.test/">Title</a>'
            '</h2><p>Snippet</p></li>'
        ))

    monkeypatch.setattr(local_search, "DDGS", SlowDDGS)

    async def run():
        # 小默认池确定性模拟慢引擎占满；保留真实搜索与 Bing 解析线程边界。
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=2))
        searches = [provider(timeout=2) for _ in range(5)]
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                for search in searches:
                    search._http_client = client
                rows = await asyncio.wait_for(asyncio.gather(
                    *(search.search("中文", 1) for search in searches),
                ), 1)
                assert len(responses) == 5
                assert all(row[0].url == "https://example.test/" for row in rows)
                assert not release.is_set()
        finally:
            release.set()
            for search in searches:
                search._http_client = None

    asyncio.run(run())
