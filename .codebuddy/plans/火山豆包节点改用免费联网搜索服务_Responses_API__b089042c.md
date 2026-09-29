---
name: 火山豆包节点改用免费联网搜索服务（Responses API）
overview: 当前 volc_ark 适配器误走 chat/completions 模型服务导致 ModelNotOpen。改为火山方舟「联网内容插件」免费搜索服务：Responses API（POST /api/v3/responses + tools web_search），解析 url_citation 标注提取搜索结果，并真实冒烟验证。
todos:
  - id: rewrite-volc-provider
    content: 重写 providers/volc_ark.py 为 Responses API 调用与 url_citation 解析
    status: completed
  - id: update-config
    content: 更新 config.json 火山节点 endpoint 与可配置 model/sources
    status: completed
    dependencies:
      - rewrite-volc-provider
  - id: smoke-adjust
    content: 临时脚本真实调用火山节点，按实际响应调整解析并删除脚本
    status: completed
    dependencies:
      - update-config
  - id: full-regression
    content: 全量回归：ruff + pytest + 四节点真实冒烟，更新 README 火山说明
    status: completed
    dependencies:
      - smoke-adjust
---

## 用户需求

修正火山豆包搜索节点的调用方式：当前实现误用 `chat/completions` 模型服务路径，导致真实调用返回 404 `ModelNotOpen`。用户指出该节点应使用火山方舟的**免费联网搜索服务（联网内容插件 / Web Search 工具）**，而非需开通的模型服务。

## 核心功能

- 将 `providers/volc_ark.py` 改为通过 **Responses API**（`POST https://ark.cn-beijing.volces.com/api/v3/responses`，`tools: [{"type": "web_search"}]`）调用免费联网搜索
- 从响应的 OpenAI Responses 风格结构中提取搜索结果（`output[]` 中 `web_search_call` 与带 `url_citation` 标注的 `message`），统一转换为 `SearchResult`
- 更新 `config.json` 火山节点 endpoint 与模型配置
- 真实冒烟验证火山节点成功返回搜索结果，并做全量回归（ruff + pytest + 四节点冒烟）
- 更新 README 中火山节点说明

## 边界

- 仅涉及 `providers/volc_ark.py`、`config.json`、`README.md` 三处，不影响其余三个节点与故障转移逻辑
- 单元测试为 mock，不受 API 变更影响

## 技术栈

沿用现有项目栈：Python 3.14 + httpx + MCP SDK（mcp 2.x MCPServer / stdio），无新增依赖。

## 问题定位（精确位置）

- `d:\搜索MCP\providers\volc_ark.py` 第 3 行 docstring、第 24-39 行请求体：走 `chat/completions` + `model=doubao-seed-2-1-pro-260628`，要求账号开通模型服务 → 404 `ModelNotOpen`
- `d:\搜索MCP\config.json` 第 36 行：`endpoint` 为 `.../api/v3/chat/completions`
- 已核实的正确调用方式（火山方舟官方文档，联网内容插件）：
- 端点：`POST https://ark.cn-beijing.volces.com/api/v3/responses`
- 鉴权：`Authorization: Bearer ark-...`（密钥本身已验证有效）
- 请求体：`{"model": "<model>", "stream": false, "tools": [{"type": "web_search"}], "input": [{"role": "user", "content": "<query>"}]}`；豆包搜索 Custom 版可在 web_search 工具中加 `"sources": ["doubao"]`
- 响应为 OpenAI Responses 风格：`output[]` 含 `web_search_call` 项与 `message` 项；搜索引用位于 `message.content[].annotations[]`（`url_citation`：url/title）；文档站为 SPA，最终响应结构须以真实调用实证并按实际调整

## 实现方案

1. **重写 volc_ark.py**：请求体改 Responses API 格式；解析逻辑改为遍历 `output[]`，聚合所有 `message.content[]` 中 `annotations` 的 `url_citation`（title/url 去重），摘要取对应 `output_text` 文本片段或 `web_search_call` 关联内容；保留基类 `_post_json`（超时/网络/非2xx/JSON 解析失败统一转 `ProviderError`，维持故障转移语义）
2. **更新 config.json**：火山节点 `options.endpoint` 改为 `/api/v3/responses`，`model` 保持可配置（默认 `doubao-seed-2-1-pro-260628`），新增可选 `sources` 配置
3. **实证调整**：先写临时探针脚本真实调用一次，按实际响应结构修正字段提取路径；若模型 ID 报错则改用其他 doubao 模型 ID 或探测 `GET /api/v3/models` 可用列表；验证完成后删除临时脚本
4. **回归与文档**：ruff + pytest（9 项 mock 测试不变）+ 四节点真实冒烟 + 更新 README 火山节点说明

## 关键代码结构（解析部分接口约定）

```python
class VolcArkProvider(SearchProvider):
    async def search(self, query: str, max_results: int) -> list[SearchResult]: ...
    def _extract_results(self, resp: dict[str, Any]) -> list[SearchResult]: ...
    # 解析约定：遍历 resp["output"]，type=="message" 项的
    # content[].annotations[] 中 type=="url_citation" 的项取 title/url，
    # 按 url 去重；摘要取 output_text 文本（截断 500 字符）。
```

## 性能与稳定性

- 单次 HTTP 请求（非流式），timeout 沿用节点配置 20s；解析为单次遍历 O(n)
- 失败时抛 `ProviderError`，由现有 `SearchRouter` 自动切换下一节点，行为与其他节点完全一致
- 日志沿用现有 logger（密钥脱敏），记录节点名/耗时/失败原因

## 目录结构（变更文件）

```
d:\搜索MCP\
├── providers\volc_ark.py  # [MODIFY] 改用 Responses API + url_citation 解析
├── config.json             # [MODIFY] volc_ark 节点 endpoint/model/sources
└── README.md               # [MODIFY] 火山节点说明（免费联网搜索服务）
```