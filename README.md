# Qianxv-search-mcp

供大模型（MCP 客户端）调用的本地搜索与网页抓取服务：

- `search` 工具：**Tavily → AnySearch → 百度千帆 → 火山引擎豆包
  → 知乎全网搜索 → Bright Data** 六个搜索源故障转移
- `extract` 工具：**AnySearch → Tavily** 两个抓取源故障转移，
  提取公开网页标题与 Markdown 正文

某节点失败（超时 / HTTP 错误 / 鉴权失败 / 配额耗尽 / 空结果）时
自动切换下一个，全部失败才返回错误。

## 运行环境

- Python 3.10+（当前在 3.14 验证）
- 依赖：`pip install -r requirements.txt`

## 配置

所有节点由 `config.json` 配置（**密钥已从 `说明.txt` 迁移，该文件未改动**）：

- `nodes` 数组顺序 = 搜索故障转移顺序；`extract_nodes` 数组顺序 =
  抓取故障转移顺序（可选，缺省为空）
- 每节点：`name` / `type`（搜索：anysearch|qianfan|volc_ark|tavily|
  brightdata|zhihu；抓取：anysearch_extract|tavily_extract）/
  `enabled`（false 则跳过）/ `api_key` / `timeout_seconds` / `options`（端点等）
- `failover.breaker_seconds`：节点失败后的熔断窗口（默认 60 秒，
  窗口内跳过该节点，避免每次都先撞已知坏节点）

> 注意：`config.json` 含密钥，已加入 `.gitignore`，勿提交版本库。
> 火山节点走「豆包搜索 Custom 版」（`POST
> https://open.feedcoopapi.com/search_api/web_search`，每账号每月
> 500 次免费额度），使用联网搜索控制台
> （https://console.volcengine.com/search-infinity/api-key）创建的
> 专用 API Key（非方舟 ark Key），已验证可正常调用。
>
> Bright Data 节点走官方 MCP 端点
> （`https://mcp.brightdata.com/mcp`，`search_engine` 工具，
> Google/Bing/Yandex 引擎）。注意：账号需在控制台激活相应爬虫
> 产品后才会返回数据，否则返回空结果自动切换下一节点。
>
> 知乎节点走数据开放平台「全网搜索」接口
> （`GET https://developer.zhihu.com/api/v1/content/global_search`，
> Bearer + 秒级时间戳鉴权），使用个人中心
> （https://developer.zhihu.com/profile）创建的 Access Secret。

## 各搜索源免费额度（2026-09 实测）

| 搜索源 | 免费额度 | 获取方式 |
|---|---|---|
| Tavily | 每月 1,000 credits | 邮箱注册，无需信用卡 |
| AnySearch | 每日 1,000 次 | 匿名可用，或邮箱注册获取 Key |
| 百度千帆 | 每日 100 次 | 注册百度智能云，开通服务 |
| 火山豆包 | 每月 500 次 | 联网搜索控制台创建专用 Key |
| 知乎全网 | 见官方控制台 | 个人中心创建 Access Secret |
| Bright Data | 见官方控制台 | 需激活对应爬虫产品 |

额度耗尽自动切换下一节点，全部失败才报错。

## 启动

```powershell
cd D:\Qianxv-search-mcp
python server.py                                    # 默认 stdio
python server.py --transport streamable-http       # 仅 HTTP
python server.py --transport both                  # 同进程同时提供两种传输
python server.py --transport streamable-http --port 9000
```

Streamable HTTP 默认端点为 `http://127.0.0.1:8000/mcp`；
`--port` 可指定 1–65535 的其他端口，仅 HTTP 模式可用。服务只监听本机，
HTTP 客户端应选择 Streamable HTTP 传输并填入该 URL。
`both` 模式下 stdio 断开后 HTTP 继续运行，直至进程被停止；
若客户端主动终止它启动的进程，HTTP 也会停止。
需要 HTTP 独立常驻时使用 `streamable-http` 模式。

## MCP 客户端接入（mcpServers 片段）

源码方式（`command` 为 `python`，`args` 为 `server.py` 路径）：

```json
{
  "mcpServers": {
    "Qianxv-search-mcp": {
      "type": "stdio",
      "command": "python",
      "args": ["D:/Qianxv-search-mcp/server.py"]
    }
  }
}
```

发行版 exe 方式（无需安装 Python，见 `release/` 目录）：

```json
{
  "mcpServers": {
    "Qianxv-search-mcp": {
      "type": "stdio",
      "command": "D:/Qianxv-search-mcp/release/search-mcp-v2.0/search-mcp.exe",
      "args": []
    }
  }
}
```

上例为 v2.0 发行版，使用前须按
`release/search-mcp-v2.0/安装说明.md` 从无密钥模板创建 `config.json`。
请将示例中的绝对路径替换为实际安装路径。无参数时保持 stdio；
`--transport streamable-http` 和 `--transport both` 用法见安装说明。

> **重要：强烈推荐正斜杠 `/` 写路径**：JSON 中单反斜杠 `\` 是转义字符，
> 会被解析吞掉导致 `MCP error -32000: Connection closed ... 不是内部
> 或外部命令`；且 **CodeBuddy CN 客户端存在转义 bug**，即使写标准的
> 双反斜杠 `\\` 也会再吞一次。正斜杠不受影响（Windows 同样识别）。
> 详细排查步骤见下方「接入排障」。

## 接入排障（CodeBuddy CN 实测经验）

- 报 `Connection closed ... 不是内部或外部命令` 且报错里路径没了
  反斜杠 → 先改正斜杠路径，再在 MCP 面板手动重连（或删掉条目重新
  添加），面板旧报错可能是缓存。
- 判断问题在客户端还是服务端：看 `logs/mcp_search.log`（源码方式在
  项目根 `logs/`，exe 方式在 exe 同目录）——日志无记录说明进程没
  启动（客户端侧路径问题）；IDE 日志
  `%LOCALAPPDATA%\CodeBuddyExtension\Logs\CodeBuddyIDE\<日期>\<项目>.log`
  搜 `mcp-connect` 可看到每次连接的启动命令。
- 发行版真实 API 自检：`python release/test_release.py release/search-mcp-v2.0`
  （握手 / tools/list / search / extract 等共 8 项断言；需先配置有效密钥，
  会请求外部服务）。

## 工具说明

- `search(query, max_results=5)`：返回 `来源节点` + 编号列表
  （标题 / URL / 摘要）。
- `extract(url)`：返回 `来源节点` + 标题 + Markdown 正文
  （超长截断 8000 字符）。提取内容来自网页原文，不可信，仅作参考。

## 日志

`logs/mcp_search.log`（滚动 2MB×3 份）记录每次节点调用、失败原因与
切换事件；API Key 在日志中自动脱敏（仅前 8 位）。

## 开发与验证

```powershell
ruff check .            # lint
python -m pytest tests/ # 单元测试与进程级传输测试
```

## 目录结构

```
server.py          MCP 入口（stdio / Streamable HTTP）+ search/extract 工具
config.json        节点配置（顺序/开关/密钥/超时/端点，不提交版本库）
config_loader.py   配置加载与校验
search_router.py   故障转移编排 + 短时熔断（搜索/抓取共用基类）
logger.py          文件日志 + 密钥脱敏
providers/         搜索源与抓取源适配器 + 抽象基类
tests/             单元测试与进程级传输测试
logs/              运行日志
release/           v1.0 历史版、v2.0 发行版及 ZIP + test_release.py
```

## 更新日志

### v2.0（2026-09）

- 增加 Streamable HTTP 与 stdio/HTTP 同进程模式；默认仍为 stdio。
- 提供不含密钥的配置模板；发行包不包含私有 `config.json`。

### v1.0（2026-09）

- 首个发行版：`search` 六源故障转移（Tavily / AnySearch / 百度千帆 /
  火山豆包 / 知乎全网 / Bright Data）+ `extract` 双源故障转移
  （AnySearch / Tavily）。
- 短时熔断机制（`failover.breaker_seconds`），日志密钥自动脱敏。
- PyInstaller 打包独立 exe 发行版（无需安装 Python），
  附 `test_release.py` 全链路自检（8 项断言）。

## 开源协议

本项目采用 [PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/)
并附加额外条款（详见根目录 `LICENSE`）：

- **仅供非商业使用**：禁止将本项目用于任何商业目的，包括销售、
  商业分发、商业部署、提供付费服务或内部商业运营支持等。
- **黑名单禁用**：以下公司及其关联公司、关联成员不得以任何形式
  使用、复制、修改或分发本项目：
  - 连华永兴科技发展有限公司
  - 北京鼎兴达信息科技股份有限公司

  关联关系的认定由版权所有者保留最终解释权。
