"""搜索/抓取节点注册表：按 config.json 构建 provider 实例列表。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .anysearch import AnySearchProvider
from .anysearch_extract import AnySearchExtractProvider
from .base import SearchProvider
from .brightdata import BrightDataProvider
from .extract_base import ExtractProvider
from .qianfan import QianfanProvider
from .tavily import TavilyProvider
from .tavily_extract import TavilyExtractProvider
from .volc_ark import VolcArkProvider
from .zhihu import ZhihuProvider

if TYPE_CHECKING:
    from config_loader import AppConfig

PROVIDER_REGISTRY: dict[str, type[SearchProvider]] = {
    "anysearch": AnySearchProvider,
    "qianfan": QianfanProvider,
    "volc_ark": VolcArkProvider,
    "tavily": TavilyProvider,
    "brightdata": BrightDataProvider,
    "zhihu": ZhihuProvider,
}

EXTRACT_REGISTRY: dict[str, type[ExtractProvider]] = {
    "anysearch_extract": AnySearchExtractProvider,
    "tavily_extract": TavilyExtractProvider,
}


def build_providers(cfg: AppConfig) -> list[SearchProvider]:
    """按配置顺序构建启用的 provider（顺序即故障转移顺序）。"""
    providers: list[SearchProvider] = []
    for node in cfg.nodes:
        if not node.enabled:
            continue
        provider_cls = PROVIDER_REGISTRY.get(node.type)
        if provider_cls is None:
            continue
        providers.append(provider_cls(node))
    return providers


def build_extract_providers(cfg: AppConfig) -> list[ExtractProvider]:
    """按配置顺序构建启用的抓取 provider（顺序即故障转移顺序）。"""
    providers: list[ExtractProvider] = []
    for node in cfg.extract_nodes:
        if not node.enabled:
            continue
        provider_cls = EXTRACT_REGISTRY.get(node.type)
        if provider_cls is None:
            continue
        providers.append(provider_cls(node))
    return providers
