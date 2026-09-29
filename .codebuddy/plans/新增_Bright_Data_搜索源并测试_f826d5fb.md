---
name: 新增 Bright Data 搜索源并测试
overview: "`说明.txt` 中新增的第 5 个数据来源为 Bright Data（API Key: 781fb2a4-...，来自 SERP/Scrapers 控制台）。按照现有 Provider 架构新增 `brightdata` 搜索适配器，注册到配置与注册表，更新 config.json / README / server.py，并完成单元测试（mock）与真实 API 冒烟测试。"
todos:
  - id: create-brightdata-adapter
    content: 创建 providers/brightdata.py 适配器（Google SERP + brd_json=1，解析 organic[]）
    status: completed
  - id: register-and-config
    content: 注册类型：config_loader.py SUPPORTED_TYPES、providers/__init__.py 注册表、config.json 追加节点
    status: completed
    dependencies:
      - create-brightdata-adapter
  - id: add-unit-tests
    content: 新增 tests/test_brightdata.py mock 单测并补充 test_router.py 配置用例
    status: completed
    dependencies:
      - create-brightdata-adapter
  - id: update-docs
    content: 更新 server.py 工具描述与 README.md 搜索链路说明
    status: completed
    dependencies:
      - register-and-config
  - id: run-tests-and-smoke
    content: 运行 pytest + ruff 全量回归，再用真实 API Key 冒烟验证 Bright Data 节点
    status: completed
    dependencies:
      - register-and-config
      - add-unit-tests
---

## 用户需求

在 `d:/搜索MCP/说明.txt` 中新增了第 5 个数据来源 **Bright Data**（API Key: `[REDACTED]`，控制台 ID `hl_ffd069b6`），需要将其作为新的搜索节点接入现有 MCP 搜索服务，并完成测试验证。

## 产品概述

现有服务为本地搜索 MCP（Python + httpx + 故障转移路由），`search` 工具在多个搜索源间自动切换。本次新增 Bright Data（SERP API，Google 引擎解析结果）作为搜索链路的第 5 个节点，失败时自动被熔断/跳过，不影响现有 4 个节点（AnySearch / 百度千帆 / 火山豆包 / Tavily）与 `extract` 抓取工具的行为。

## 核心功能

- 新增 `brightdata` 搜索适配器：调用 Bright Data SERP API（`POST https://api.brightdata.com/request`，Bearer 鉴权，`brd_json=1` 解析 JSON），将 `organic[]` 的 `title/link/description` 映射为标准 `SearchResult`
- 注册新节点类型并追加到 `config.json` 的 `nodes` 末尾（故障转移优先级最低，保护额度），zone 等参数可配置
- 单元测试（mock，不发真实请求）+ 真实 API 冒烟测试，确认返回真实搜索结果
- 同步更新 `server.py` 工具描述与 `README.md` 文档

## Tech Stack

- 复用现有栈：Python 3.10+ / httpx / dataclass / pytest / ruff（不引入新依赖）
- 复用 `providers/base.py` 的 `SearchProvider` 抽象、`_post_json()`（超时/网络/HTTP/JSON 错误统一转 `ProviderError`）、`SearchResult` 标准结构

## Implementation Approach

按现有 Provider 插件式架构扩展（与 tavily.py 同构）：

1. **适配器** `providers/brightdata.py`：校验 `endpoint`/`api_key`/`zone` 后构造请求体 `{"zone": zone, "url": "https://www.google.com/search?q=<urlencode(query)>&num=<n>&hl=<lang>&brd_json=1", "format": "raw"}`，复用 `_post_json()`；解析响应中 `organic[]`，取 `title`/`link`/`description`（注意：字段名是 `description` 而非 `snippet`），截断至 500 字符并限制 `max_results`；无 `organic` 或空结果返回空列表触发上层故障转移。
2. **注册**：`config_loader.py` 的 `SUPPORTED_TYPES` 加入 `"brightdata"`；`providers/__init__.py` 的 `PROVIDER_REGISTRY` 注册映射。
3. **配置**：`config.json` 追加节点 `{name: "brightdata", type: "brightdata", enabled: true, api_key: 781fb2a4-..., timeout_seconds: 30, options: {endpoint: "https://api.brightdata.com/request", zone: "hl_ffd069b6", language: "zh-CN", search_engine: "google"}}`，置于 nodes 末尾。
4. **文档**：更新 `server.py` search 工具 docstring 来源列表、README 搜索链路描述。
5. **测试**：新增 mock 单测（响应解析/字段映射/缺 api_key/缺 zone/空 organic/HTTP 错误转 ProviderError）+ 全量回归 `pytest` + `ruff check`，最后真实 API 冒烟（临时脚本单独实例化 BrightDataProvider 直调，避免消耗其他节点配额；验证 zone 与响应格式，若返回原始 HTML 或鉴权异常按备选异步端点 `POST /serp/req` + `GET /unblocker/get_result` 轮询方案调整）。

### 关键决策

- **同步端点优先**：`POST /request` 一次往返拿解析 JSON，实现最简；异步轮询（最长 5 分钟）与 MCP 同步工具场景不匹配，仅作降级备选。
- **节点置于末尾**：Bright Data 按量计费，放故障转移链末位，仅在其他 4 个节点全部失败时兜底。
- **timeout 30s**：SERP 抓取+解析耗时高于纯 API 搜索（其他节点 10-20s）。

## Implementation Notes

- 响应体可能为非 dict（解析 JSON 失败/HTML），`_post_json` 已处理非 dict 场景；适配器对 `organic` 缺失返回空列表即可，勿抛非 ProviderError 异常
- 错误信息截断前 200 字符，不打印完整响应（防止日志泄漏/膨胀）；API Key 脱敏已由现有 logger 处理
- 请求 URL 的 query 必须 `urllib.parse.quote` 编码，中文查询词直接拼接会被服务端拒绝
- `说明.txt` 保持不改动（既有约定：密钥已迁移至 config.json）
- 冒烟测试若 zone `hl_ffd069b6` 无效（401/404），需提示用户在控制台确认 zone 名称，而非猜测

## Directory Structure

```
d:/搜索MCP/
├── providers/
│   ├── brightdata.py      # [NEW] Bright Data SERP 适配器：构造 Google 搜索 URL + brd_json=1，
│   │                      #   复用 _post_json，解析 organic[] -> SearchResult；校验 endpoint/api_key/zone
│   └── __init__.py        # [MODIFY] PROVIDER_REGISTRY 增加 "brightdata": BrightDataProvider
├── config_loader.py       # [MODIFY] SUPPORTED_TYPES 增加 "brightdata"（第 12 行）
├── config.json            # [MODIFY] nodes 末尾追加 brightdata 节点（含 api_key/zone/endpoint/timeout 30s）
├── server.py              # [MODIFY] search 工具 docstring 来源列表加入 Bright Data
├── README.md              # [MODIFY] 搜索链路说明更新为五个搜索源
├── tests/
│   └── test_brightdata.py # [NEW] 单测：mock _post_json，覆盖正常解析/空 organic/缺 key/缺 zone/HTTP 错误
└── tests/test_router.py   # [MODIFY] 配置测试中补充 brightdata 类型的合法节点用例
```

## Key Code Structures

```python
class BrightDataProvider(SearchProvider):
    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        # 校验 endpoint/api_key/zone -> 构造 body
        # data = await self._post_json(self.endpoint, body)
        # 解析 data.get("organic") -> SearchResult(title, link, description, source=self.name)
```