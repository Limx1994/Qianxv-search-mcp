"""配置加载与校验：读取 config.json 生成节点配置对象。"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _app_dir() -> Path:
    """应用基准目录：PyInstaller frozen 时为 exe 所在目录，否则为源码目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_PATH = _app_dir() / "config.json"

SUPPORTED_TYPES = {
    "anysearch",
    "qianfan",
    "volc_ark",
    "tavily",
    "brightdata",
    "zhihu",
}
SUPPORTED_EXTRACT_TYPES = {"anysearch_extract", "tavily_extract"}


class ConfigError(Exception):
    """配置文件缺失或格式非法。"""


@dataclass
class NodeConfig:
    """单个搜索节点配置。"""

    name: str
    type: str
    enabled: bool
    api_key: str
    timeout_seconds: float
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class AppConfig:
    """全局配置：熔断窗口 + 有序搜索节点 + 有序抓取节点列表。"""

    breaker_seconds: float
    nodes: list[NodeConfig]
    extract_nodes: list[NodeConfig] = field(default_factory=list)


def _parse_node_list(
    raw_nodes: object, supported: set[str], section: str
) -> list[NodeConfig]:
    """解析并校验节点数组；顺序即故障转移顺序。"""
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ConfigError(f"'{section}' must be a non-empty array")
    nodes: list[NodeConfig] = []
    seen_names: set[str] = set()
    for idx, item in enumerate(raw_nodes):
        if not isinstance(item, dict):
            raise ConfigError(f"{section}[{idx}] must be an object")
        name = str(item.get("name", "")).strip()
        node_type = str(item.get("type", "")).strip()
        if not name or name in seen_names:
            raise ConfigError(
                f"{section}[{idx}] has empty or duplicate name"
            )
        if node_type not in supported:
            raise ConfigError(
                f"node '{name}' unsupported type '{node_type}'"
            )
        seen_names.add(name)
        options = item.get("options", {}) or {}
        if not isinstance(options, dict):
            raise ConfigError(f"node '{name}' options must be an object")
        nodes.append(
            NodeConfig(
                name=name,
                type=node_type,
                enabled=bool(item.get("enabled", True)),
                api_key=str(item.get("api_key", "")),
                timeout_seconds=float(item.get("timeout_seconds", 10.0)),
                options=options,
            )
        )
    return nodes


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    """读取并校验 config.json，返回 AppConfig。"""
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from exc

    failover = raw.get("failover", {}) or {}
    breaker_seconds = float(failover.get("breaker_seconds", 60))

    nodes = _parse_node_list(raw.get("nodes"), SUPPORTED_TYPES, "nodes")
    extract_nodes: list[NodeConfig] = []
    if raw.get("extract_nodes") is not None:
        extract_nodes = _parse_node_list(
            raw.get("extract_nodes"),
            SUPPORTED_EXTRACT_TYPES,
            "extract_nodes",
        )
    return AppConfig(
        breaker_seconds=breaker_seconds,
        nodes=nodes,
        extract_nodes=extract_nodes,
    )
