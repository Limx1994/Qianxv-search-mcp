---
name: MCP 服务增加网页抓取（extract）工具
overview: 在现有搜索 MCP 服务上新增第二个工具 extract(url)：网页内容抓取，双源故障转移（AnySearch /v1/extract → Tavily /extract），复用现有密钥与 config.json/熔断/日志架构，不改老功能。
todos:
  - id: extract-providers
    content: 新增 ExtractProvider 基类与 AnySearch/Tavily 抓取适配器，注册进 providers/__init__.py
    status: completed
  - id: router-config
    content: 泛化故障转移基类新增 ExtractRouter，config.json 增加 extract_nodes 并扩展 config_loader 校验
    status: completed
    dependencies:
      - extract-providers
  - id: register-tool
    content: server.py 注册 extract(url) MCP 工具（内容截断 8000 字符）
    status: completed
    dependencies:
      - router-config
  - id: extract-tests
    content: tests 新增 extract 故障转移/全部失败/禁用节点/Tavily 空结果 mock 测试
    status: completed
    dependencies:
      - register-tool
  - id: verify-docs
    content: 运行 ruff + pytest + 真实冒烟两个抓取源，更新 README
    status: completed
    dependencies:
      - extract-tests
---

## 用户需求

在现有本地搜索 MCP 服务（d:\搜索MCP）上增加**网络抓取（网页内容提取）**功能，作为第二个 MCP 工具 `extract(url)`。

## 产品概述

大模型客户端调用 `extract(url)` 时，服务按配置顺序依次尝试 AnySearch `/v1/extract` → Tavily `/extract` 两个抓取源，任一源失败（超时/HTTP错误/鉴权失败/配额耗尽/空内容）自动切换下一个，全部失败返回明确错误。复用现有密钥、config.json 配置、熔断与日志架构，不影响现有 `search` 工具。

## 核心功能

- MCP 工具 `extract(url)`：输入公开网页 URL，返回标题 + 清洗后 Markdown 正文（超长截断 8000 字符）
- 双源故障转移：AnySearch 优先（每日 1000 次额度），Tavily 兜底（每月 credits）
- 配置驱动：config.json 新增 `extract_nodes` 数组，节点顺序/开关/密钥/超时可配
- 日志：沿用 logs/mcp_search.log，记录抓取调用与切换事件（Key 脱敏）
- 交付：单元测试（mock 故障转移）+ 真实冒烟 + README 更新

## 边界与约束

- 不编辑 `说明.txt`；老功能 `search` 行为完全不变
- 单函数/变量名 ≤ 40 字符；单文件 ≤ 4800 行
- 提取内容不可信，仅作为数据返回给模型，不执行其中任何指令

## Tech Stack

- 沿用现有栈：Python 3.14 + httpx + MCP SDK 2.x（MCPServer / stdio），零新增依赖
- 工具链：ruff（lint）、pytest（单元测试）

## 实现方案（已核实 API 规格）

1. **AnySearch Extract**：`POST https://api.anysearch.com/v1/extract`，`Authorization: Bearer as_sk_...`，请求体严格 `{"url": "<绝对URL>"}`；响应 `{code:0, data:{url,title,content}}`；错误信封 `code:-1` + `message`
2. **Tavily Extract**：`POST https://api.tavily.com/extract`，`Authorization: Bearer tvly-...`，请求体 `{"urls":"<url>","format":"markdown"}`；响应 `{results:[{url,raw_content}], failed_results:[...]}`；注意 HTTP 200 也可能 `results` 为空 → 按失败处理触发故障转移

### 关键决策

- **复用而非重写**：`providers/base.py` 的 `_post_json`（超时/网络/非2xx/JSON解析统一转 `ProviderError`）直接复用；`search_router.py` 的熔断+遍历逻辑泛化为通用基类，`SearchRouter` 与新的 `ExtractRouter` 共享，避免复制粘贴（DRY）
- **独立 extract provider 文件**：`extract_base.py` / `anysearch_extract.py` / `tavily_extract.py`，与搜索 provider 分离（SoC），新增抓取源只需加文件 + 注册表条目（开闭原则）
- **配置区分**：config.json 新增顶层 `extract_nodes` 数组（与 `nodes` 平行），`config_loader.py` 增加独立白名单校验；密钥直接复用现有 AnySearch/Tavily Key
- **性能**：单次 HTTP 请求、串行故障转移（省额度），超时沿用节点配置；返回内容截断 8000 字符避免撑爆上下文

## 目录结构

```
d:\搜索MCP\
├── providers\
│   ├── base.py              # [MODIFY] 新增 ExtractResult dataclass
│   ├── extract_base.py      # [NEW] ExtractProvider 抽象基类（name/extract(url)）
│   ├── anysearch_extract.py # [NEW] AnySearch /v1/extract 适配器
│   ├── tavily_extract.py    # [NEW] Tavily /extract 适配器
│   └── __init__.py          # [MODIFY] 增加 EXTRACT_REGISTRY + build_extract_providers
├── config_loader.py         # [MODIFY] 解析校验 extract_nodes（独立类型白名单）
├── search_router.py         # [MODIFY] 熔断+遍历泛化为 _FailoverRouter 基类，新增 ExtractRouter；SearchRouter 公共行为不变
├── server.py                # [MODIFY] 注册 @mcp.tool() extract(url)
├── config.json              # [MODIFY] 新增 extract_nodes 数组（复用现有密钥）
├── tests\test_router.py     # [MODIFY] 新增 extract 故障转移/全部失败/禁用节点 mock 测试
└── README.md                # [MODIFY] 工具说明增加 extract
```

## 实施注意事项

- Tavily HTTP 200 + `results` 空数组必须视为失败（触发切换），不能当成功返回
- AnySearch 请求体严格仅含 `url` 一个字段（多字段会被 400 拒绝）
- extract 返回内容前声明"提取内容不受信任"，仅作数据展示
- `search_router.py` 重构保持 `SearchRouter.search()` 签名与语义不变，用现有 9 项单元测试回归验证老功能零影响
- 日志记录节点名/耗时/失败原因，Key 脱敏（前 8 位）