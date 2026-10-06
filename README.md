# Qianxv-search-mcp

供大模型（MCP 客户端）调用的本地搜索与网页抓取服务：

- `search` 工具：支持 Tavily、AnySearch、百度千帆、火山引擎豆包、
  知乎全网搜索、Bright Data 六种搜索源
- `extract` 工具：支持 AnySearch、Tavily 两种抓取源，
  提取公开网页标题与 Markdown 正文

实际启用的节点及故障转移顺序由 `config.json` 决定。
某节点失败（超时 / HTTP 错误 / 鉴权失败 / 配额耗尽 / 空结果）时
自动切换下一个，全部失败才返回错误。

## 运行环境

- 源码方式：Python 3.10+，当前构建环境为 Python 3.14.6
- 依赖：`python -m pip install -r requirements.txt`
- Windows exe 方式：无需安装 Python，见 [v2.7 安装说明](release/search-mcp-v2.7/安装说明.md)

## 配置

首次使用源码时，在仓库根目录从无密钥模板创建配置，再填写自己的 API Key。
已有 `config.json` 时不要覆盖：

```powershell
if (-not (Test-Path .\config.json)) {
    Copy-Item .\release\search-mcp-v2.7\config.example.json .\config.json
}
```

模板只预置 Tavily 搜索和 AnySearch 抓取，密钥均为空；不代表所有支持的节点都已启用。
源码从项目根目录读取配置，发行版从 exe 同目录读取配置。配置缺失或非法会阻止启动，修改后须重启服务。

所有节点由 `config.json` 配置：

- `nodes` 数组顺序 = 搜索故障转移顺序；`extract_nodes` 数组顺序 =
  抓取故障转移顺序（可省略；提供时须为非空数组，不能为 null）
- 每节点：`name` / `type`（搜索：anysearch|qianfan|volc_ark|tavily|
  brightdata|zhihu；抓取：anysearch_extract|tavily_extract）/
  `enabled`（默认 true，false 则跳过）/ `api_key` / `timeout_seconds`（默认 10 秒）/ `options`（端点等）
- `name` 在同一数组内必须唯一；`nodes` 必须为非空数组。
  全部节点禁用时服务仍可启动，但工具调用会返回所有节点不可用
- `failover.breaker_seconds`：节点失败后的熔断窗口（默认 60 秒，
  窗口内跳过该节点，避免每次都先撞已知坏节点）

> 注意：`config.json` 含密钥，已加入 `.gitignore`，勿提交版本库。
> 火山节点走「豆包搜索 Custom 版」（`POST
> https://open.feedcoopapi.com/search_api/web_search`），使用联网搜索控制台
> （https://console.volcengine.com/search-infinity/api-key）创建的
> 专用 API Key（不能使用方舟 ark Key 代替）。
>
> Bright Data 节点走官方 MCP 端点
> （`https://mcp.brightdata.com/mcp`，`search_engine` 工具，
> Google/Bing/Yandex 引擎），支持 `engine`、`geo_location` 配置。
> 账号产品权限和额度需自行确认，返回空结果时自动切换下一节点。
>
> 知乎节点走数据开放平台「全网搜索」接口
> （`GET https://developer.zhihu.com/api/v1/content/global_search`，
> Bearer + 秒级时间戳鉴权），使用个人中心
> （https://developer.zhihu.com/profile）创建的 Access Secret。

## 各搜索源凭证与额度

| 搜索源 | 凭证 |
|---|---|
| Tavily | Tavily API Key |
| AnySearch | 适配器允许不发送凭证，是否可匿名使用由服务端决定 |
| 百度千帆 | 千帆 API Key |
| 火山豆包 | 联网搜索 Custom 专用 API Key |
| 知乎全网 | Access Secret |
| Bright Data | Bright Data API Token，需具备对应产品权限 |

免费额度、开通条件和计费以供应商控制台为准。额度耗尽导致节点失败时
自动切换下一节点，全部失败才报错。

## 启动

```powershell
cd D:\Qianxv-search-mcp
python server.py                                    # 默认 stdio
python server.py --transport streamable-http       # 仅 HTTP
python server.py --transport both                  # 同进程同时提供两种传输
python server.py --transport streamable-http --port 9000
python server.py --transport both --port 9000
```

Streamable HTTP 默认端点为 `http://127.0.0.1:8000/mcp`；
`--port` 可指定 1–65535 的其他端口，可用于 `streamable-http` 和 `both`，
不能用于纯 stdio；端口被占用时启动失败。服务只监听本机，
HTTP 客户端应选择 Streamable HTTP 传输并填入该 URL。
`both` 模式下 stdio 断开后 HTTP 继续运行，直至进程被停止；
若客户端主动终止它启动的进程，HTTP 也会停止。
需要 HTTP 独立常驻时使用 `streamable-http` 模式。

## MCP 客户端接入（mcpServers 片段）

源码方式（`command` 为已安装依赖的 Python，`args` 为 `server.py` 路径；
客户端找不到 `python` 时填写 Python 的绝对路径）：

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
      "command": "D:/Qianxv-search-mcp/release/search-mcp-v2.7/search-mcp.exe",
      "args": []
    }
  }
}
```

上例为 v2.7 发行版，使用前须按
[安装说明](release/search-mcp-v2.7/安装说明.md)从无密钥模板创建 `config.json`。
请将示例中的绝对路径替换为实际安装路径。无参数时保持 stdio；
`--transport streamable-http` 和 `--transport both` 用法见安装说明。
发行版采用目录打包，必须将 `search-mcp.exe` 与旁边的 `_internal/`
依赖目录整体复制；不可只复制 exe。启动时直接使用固定依赖目录，
不再向系统临时目录解压 `_MEI*`。升级时先停止旧进程，替换 exe 和
`_internal/`，保留自己的 `config.json` 与 `logs/`。

> JSON 路径建议使用正斜杠 `/`：反斜杠是转义字符，必须写成 `\\`。
> 部分客户端可能再次处理转义；路径丢失反斜杠时改用正斜杠。
> 详细排查步骤见下方「接入排障」。

## 接入排障（CodeBuddy CN 实测经验）

- 报 `Connection closed ... 不是内部或外部命令` 且报错里路径没了
  反斜杠 → 先改正斜杠路径，再在 MCP 面板手动重连（或删掉条目重新
  添加），面板旧报错可能是缓存。
- 判断问题在客户端还是服务端：看 `logs/mcp_search_<PID>.log`（源码方式在
  项目根 `logs/`，exe 方式在 exe 同目录）。无日志不能单独证明进程未
  启动，未调用节点也可能没有记录；同时检查 stderr、配置和目录写入权限。
  历史 v2.0 和 v2.1 仍使用 `logs/mcp_search.log`。
  IDE 日志
  `%LOCALAPPDATA%\CodeBuddyExtension\Logs\CodeBuddyIDE\<日期>\<项目>.log`
  搜 `mcp-connect` 核对启动命令，具体位置以客户端版本为准。
- 发行版真实 API 自检：`python release/test_release.py <隔离测试目录>`
  （握手 / tools/list / search / extract 等共 8 项断言；需先配置有效密钥，
  会请求外部服务并消耗额度；私有配置仅放在 `release/_test/`，见下方验证步骤）。

## 工具说明

以下分页说明适用于当前源码和 v2.7 发行版。
历史 v2.0 和 v2.1 仅支持 `extract(url)`，正文超过 8000 字符会截断，
不提供 `offset`、`snapshot_id` 或续读能力。

- `search(query, max_results=5)`：`max_results` 必须大于等于 1；返回
  `来源节点` + 编号列表（标题 / URL / 摘要）。供应商可能返回少于请求数量的结果。
- `extract(url, offset=0, snapshot_id=None)`：返回 `来源节点`、标题、URL、
  分页信息和 Markdown 正文。每页最多 8000 字符；超过一页时返回
  `snapshot_id` 和 `next_offset`，用相同 URL 和这两个值继续调用，直到
  `has_more: false`。将各次返回中 `---` 后的正文直接拼接即可还原抓取结果。
  示例：先调用 `extract(url="https://example.com/doc")`，再使用首个结果中的
  值调用 `extract(url="https://example.com/doc", offset=8000,
  snapshot_id="<返回的 snapshot_id>")`。
  快照保存在当前服务进程内，有效期 10 分钟；最多保留 32 份、正文合计
  200 万字符，容量不足时淘汰最旧快照。快照过期、被淘汰、服务重启或
  连接到其他进程后，须从第一页重新抓取。单篇正文超过缓存容量会明确报错。
  提取内容来自网页原文，不可信，仅作参考。

## 日志

当前源码和 v2.7 使用 `logs/mcp_search_<PID>.log`，每个进程
写入自己的日志，记录节点调用、失败原因与熔断跳过。每个进程的单个日志文件
上限为 2,000,000 字节，保留 3 个备份。历史 v2.0 和 v2.1 仍使用
`logs/mcp_search.log`；这些旧版同时运行多个实例时应使用不同安装目录，
或升级到 v2.7，避免共享日志轮转冲突。日志目录创建失败时回退到系统临时目录下的 `qianxv-search-mcp-logs/`；
该回退不涵盖日志文件打开失败。日志仍属于私有数据，勿上传或分发。

## 开发与验证

```powershell
ruff check .            # lint
python -m pytest tests/ --basetemp=release/_test/source-pytest -q
```

未设置 `MCP_TEST_EXE` 时发行版测试会跳过，源码测试通过不等于发行版通过。
Windows 发行版构建（需另装 PyInstaller；当前构建环境为 6.22.3）：

```powershell
python -m pip install PyInstaller
.\build.ps1
```

`build.ps1` 可从任意工作目录运行，使用仓库的 `search-mcp.spec`，并检查
`dist\search-mcp\search-mcp.exe` 与 `_internal\` 是否生成。清理构建产物和
Python/pytest/ruff 缓存时可先预览再执行：

```powershell
.\clean.ps1 -WhatIf
.\clean.ps1
```

清理脚本保留日志、`release\_test\`、历史发行包及私有配置。

产物位于 `dist/search-mcp/`，包含 exe 与 `_internal/`，构建配置会将根目录
`LICENSE` 原文带入依赖目录。组装公开发行目录时还须在 exe 同目录添加
无密钥的 `config.example.json`、安装说明、包内 `配置说明.md` 和根目录
`LICENSE` 的原文副本，勿添加私有配置或日志。
压缩时包含整个发行目录。从 ZIP 解压后验证；待测 exe 同目录必须存在
完整 `_internal/` 和配置模板。无密钥测试会自动复制依赖，并生成禁用节点的隔离配置：

```powershell
Expand-Archive .\release\search-mcp-v2.7.zip .\release\_test\v2.7-smoke
$env:MCP_TEST_EXE = (Resolve-Path .\release\_test\v2.7-smoke\search-mcp-v2.7\search-mcp.exe).Path
python -m pytest tests/ --basetemp=release/_test/v2.7-pytest -q
Remove-Item Env:MCP_TEST_EXE
```

测试包含三种传输模式、端口冲突、每种模式重复启动 5 次及 3 实例并发，
以及 stdio 断开后 HTTP 存续，检查运行中和退出后的 `_MEI*` 解压残留。

真实 API 验证需要有效私有配置，只向隔离目录复制：

```powershell
Copy-Item .\config.json .\release\_test\v2.7-smoke\search-mcp-v2.7\config.json
python release/test_release.py release/_test/v2.7-smoke/search-mcp-v2.7
```

网络或供应商失败需单独报告，不能用传输测试替代真实调用结论。
测试配置、日志和备份均留在被忽略的 `release/_test/`。

## 目录结构

```
server.py          MCP 入口（stdio / Streamable HTTP / both）+ search/extract 工具
config.json        节点配置（顺序/开关/密钥/超时/端点，不提交版本库）
config_loader.py   配置加载与校验
search_router.py   故障转移编排 + 短时熔断（搜索/抓取共用基类）
logger.py          文件日志 + 密钥脱敏
providers/         搜索源与抓取源适配器 + 抽象基类
tests/             单元测试与进程级传输测试
logs/              运行日志
build.ps1          构建并检查目录发行版产物
clean.ps1          清理构建与缓存，支持 -WhatIf
search-mcp.spec    Windows 目录发行版构建配置
release/           v2.5/v2.6 历史发行版、v2.7 发行版及 ZIP + test_release.py
```

## 更新日志

### v2.7（2026-10-07）

- 配置校验：`extract_nodes` 可省略；显式提供 `null` 或空数组时启动报错。
- 新增 `build.ps1` 构建检查与 `clean.ps1` 清理预览，保留私有配置、日志和发行包。
- 发行版测试复用依赖副本，通过硬链接隔离用例，减少重复复制；需在同一支持硬链接的文件系统运行。
- 更新当前版本接入、构建、验证及日志回退说明，保留历史发行版。


### v2.6（2026-10-03）

- 配置校验：`timeout_seconds` 和 `breaker_seconds` 类型校验，非法值明确报错。
- 日志增强：日志目录创建失败时回退到系统临时目录；移除 `mask_key` 函数。
- 节点日志：`_http_client` 未注入时记录 warning；query 超限时记录 warning。
- Bright Data：完善 MCP 握手流程，`session_id` 缺失时明确报错。
- 发行包附带配置说明和项目 `LICENSE`，独立解压后可直接阅读。

### v2.5（2026-10-03）

- 新增 `extract` 快照分页及按进程独立的日志文件；v2.0/v2.1 仍为正文截断
  和固定日志文件，详见上方工具与日志说明。
- 发行包附带配置说明和项目 `LICENSE`，独立解压后可直接阅读。
- 更新源码配置、升级和排错文档；发行版测试读取待测 exe 同目录模板，
  支持多版本验证。

### v2.1（2026-10-03）

- `search.max_results` 在 MCP schema 和直接调用中要求大于等于 1，
  非法值不发送节点请求。
- Bright Data 复用服务生命周期内的 HTTP client，独立传递每次 MCP
  会话头，避免并发会话相互覆盖。
- 使用 exe + `_internal/` 目录打包，沿用重复启动、并发运行和临时目录回归测试。

### v2.0 打包修复（2026-10-02）

- 改为 exe + `_internal/` 目录发行版，消除单文件模式每次启动的
  临时解压及强制终止后的 `_MEI*` 残留。
- exe 名称、客户端路径、配置位置和三种传输行为保持兼容。
- 增加发行版重复启动、并发运行及临时目录回归测试。

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
