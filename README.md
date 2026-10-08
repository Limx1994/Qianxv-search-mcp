# Qianxv-search-mcp

供大模型（MCP 客户端）调用的本地搜索与网页抓取服务：

- `search` 工具：支持 Tavily、AnySearch、百度千帆、火山引擎豆包、
  知乎全网搜索和本机 DDGS 搜索
- `extract` 工具：支持 AnySearch、Tavily 和本机 HTML 抓取，
  提取公开网页标题与 Markdown 正文

实际启用的节点及故障转移顺序由 `config.json` 决定。
某节点失败（超时 / HTTP 错误 / 鉴权失败 / 配额耗尽 / 空结果）时
自动切换下一个。全部节点真实失败或总预算耗尽才返回 MCP 工具错误；
尝试结束后没有有效结果、但至少一个节点正常响应时，返回“未找到相关结果”。
搜索空结果不触发熔断。

## 运行环境

- 源码方式：Python 3.10+，当前构建环境为 Python 3.14.6
- 依赖：`python -m pip install -r requirements.txt`
- Windows exe 方式：无需安装 Python，见 [v2.10 安装说明](release/search-mcp-v2.10/安装说明.md)

## 配置

首次使用源码时，在仓库根目录从无密钥模板创建配置；云节点填写自己的 API Key，
本机节点无需密钥。
已有 `config.json` 时不要覆盖：

```powershell
if (-not (Test-Path .\config.json)) {
    Copy-Item .\config.example.json .\config.json
}
```

根目录模板预置 Tavily 搜索、AnySearch 抓取，并在各数组末尾启用本机兜底，密钥均为空。
v2.10 模板与根目录模板一致；历史 v2.8/v2.9 模板只包含云节点，旧版 exe 不支持本机节点类型。
源码从项目根目录读取配置，发行版从 exe 同目录读取配置。配置缺失或非法会阻止启动，修改后须重启服务。

所有节点由 `config.json` 配置：

- `nodes` 数组顺序 = 搜索故障转移顺序；`extract_nodes` 数组顺序 =
  抓取故障转移顺序（可省略；提供时须为非空数组，不能为 null）
- 每节点：`name` / `type`（搜索：anysearch|qianfan|volc_ark|tavily|
  zhihu|local_search；抓取：anysearch_extract|tavily_extract|local_extract）/
  `enabled`（默认 true，false 则跳过）/ `api_key` / `timeout_seconds`（本机抓取默认 15 秒，其余默认 10 秒）/ `options`（端点等）
- `name` 在同一数组内必须唯一；`nodes` 必须为非空数组。
  全部节点禁用时服务仍可启动，但工具调用会返回所有节点不可用
- `failover.breaker_seconds`：节点失败后的熔断窗口（默认 60 秒，
  窗口内跳过该节点，避免每次都先撞已知坏节点）；必须为有限非负数，`0` 禁用熔断。
  冷却结束后只允许一个恢复探测，其余并发请求走备用节点。
- `timeout_seconds` 必须为有限正数，同时限制 HTTP 网络阶段和整个节点执行过程；
  `enabled` 必须为 JSON 布尔值。根配置与 `failover` 须为对象。
- `failover.search_timeout_seconds` / `failover.extract_timeout_seconds`：可选的整次调用
  总预算，必须为有限正数；省略时为对应启用节点的 `timeout_seconds` 合计。
  模板省略这两个字段，新增节点会自动计入默认预算。实际节点上限取节点超时与剩余
  总预算的较小值，预算耗尽后停止尝试；客户端取消不触发节点熔断。

> 注意：`config.json` 含密钥，已加入 `.gitignore`，勿提交版本库。
> 火山节点走「豆包搜索 Custom 版」（`POST
> https://open.feedcoopapi.com/search_api/web_search`），使用联网搜索控制台
> （https://console.volcengine.com/search-infinity/api-key）创建的
> 专用 API Key（不能使用方舟 ark Key 代替）。
>
> Bright Data 因使用门槛高，当前版本暂时移除，不再支持 `brightdata` 节点。
> 旧配置升级前请删除该节点，其余节点与凭证保留。
>
> 知乎节点走数据开放平台「全网搜索」接口
> （`GET https://developer.zhihu.com/api/v1/content/global_search`，
> Bearer + 秒级时间戳鉴权），使用个人中心
> （https://developer.zhihu.com/profile）创建的 Access Secret。

## 无需云 API 的本机兜底

`local_search` 通过 [DDGS](https://pypi.org/project/ddgs/) 在本机访问搜索引擎，
`local_extract` 使用共享 `httpx` 客户端下载网页，再通过
[Trafilatura](https://trafilatura.readthedocs.io/) 在本机提取标题和 Markdown 正文。
两个节点均不使用 API key，仍需联网；只启用这两个节点也可运行。

- 将根目录模板中的本机节点追加到现有配置对应数组末尾，重启后即可兜底。
  保留已有云节点和凭证；不要用模板覆盖私有配置。
- 搜索默认超时 10 秒，`options.backend` 默认 `auto`（`all` 等价）。每次查询并发
  尝试当前 DDGS 版本启用的全部 text 引擎和 `bing_html`，返回首个非空有效结果；
  失败或空结果不会阻止其他引擎，全部失败时明确报告。所有尝试共享节点超时，
  不按测试机器固化可用引擎，也不将超时时间乘以引擎数量。
  DDGS 9.16.0 启用的 text 引擎为 `brave`、`duckduckgo`、`google`、`grokipedia`、
  `mojeek`、`startpage`、`wikipedia`、`yahoo`，实际可用性取决于运行机器的网络。
  `backend` 可指定单个引擎或逗号分隔的引擎列表（如 `duckduckgo,google,bing_html`），
  限定尝试范围；列表同样返回首个有效结果，单个引擎不会自动换用其他引擎。
  `options.region` 默认 `cn-zh`。未知和禁用引擎明确报错，`auto`/`all` 必须单独使用。
  `bing_html` 通过共享 `httpx` 下载 Bing HTML 并复用 DDGS 解析器；它独立于 DDGS
  已禁用的原生 `bing` 和 `yandex` text 后端。DDGS 代理通过 `DDGS_PROXY` 配置；`bing_html`
  和抓取沿用 `httpx` 的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY`。
- 抓取默认超时 15 秒，单页下载上限 5 MiB，仅接受 HTTP/HTTPS HTML 页面，
  最多跟随 5 次重定向；每一跳均检查 URL，拒绝 localhost、直接输入的非公网 IP
  和带凭证 URL。不向目标发送 API key 或 cookies。
- 抓取保留原始域名，使用标准 `httpx` 网络行为：有代理按代理配置请求，
  `NO_PROXY` 匹配时直连，没有代理时使用系统 DNS 和正常连接。
  支持 HTTP、HTTPS、SOCKS5 和 SOCKS5h 代理，代理认证沿用环境配置；
  目标域名可以由代理解析，不要求本机能够解析代理端目标。
  HTTP 和 HTTPS 均不设置机器专属 DNS 网段或代理信任名单。
  旧的 `trusted_dns_networks` 和 `trusted_proxy_urls` 选项不再影响请求。
  HTTPS 始终校验证书，目标和 HTTPS 代理分别使用各自的 TLS 身份；
  证书信任沿用 `httpx` 的 `SSL_CERT_FILE`、`SSL_CERT_DIR` 配置。
- 首版不运行 JavaScript，不处理登录或验证码，也不提供 PDF 提取、离线索引。
  搜索引擎不可达、限流或网页无法提取时会明确失败；本机兜底不能保证任意网站可用。
- 默认总预算会累加新增节点超时。若显式设置整次预算，须为末尾兜底留出时间；
  预算耗尽会停止尝试，客户端超时也应覆盖服务端预算。云节点原有超时仍决定兜底前
  的最长等待时间。
- DDGS 搜索和正文解析在线程中执行，Bing 下载使用异步 HTTP。取消或节点超时可结束等待，底层同步任务可能持续到
  自身执行结束或网络超时。

## 各搜索源凭证与额度

| 搜索源 | 凭证 |
|---|---|
| Tavily | Tavily API Key |
| AnySearch | 适配器允许不发送凭证，是否可匿名使用由服务端决定 |
| 百度千帆 | 千帆 API Key |
| 火山豆包 | 联网搜索 Custom 专用 API Key |
| 知乎全网 | Access Secret |
| 本机 DDGS / HTML 抓取 | 无需凭证，需能访问搜索引擎或目标网页 |

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
      "command": "D:/Qianxv-search-mcp/release/search-mcp-v2.10/search-mcp.exe",
      "args": []
    }
  }
}
```

上例为 v2.10 发行版，使用前须按
[安装说明](release/search-mcp-v2.10/安装说明.md)从无密钥模板创建 `config.json`。
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

以下分页说明适用于当前源码和 v2.10 发行版。
历史 v2.0 和 v2.1 仅支持 `extract(url)`，正文超过 8000 字符会截断，
不提供 `offset`、`snapshot_id` 或续读能力。

- `search(query, max_results=5)`：query 不得为空或纯空白；`max_results` 必须大于等于 1；返回
  `来源节点` + 编号列表（标题 / URL / 摘要）。供应商可能返回少于请求数量的结果。
- `extract(url, offset=0, snapshot_id=None)`：返回 `来源节点`、标题、URL、
  分页信息和 Markdown 正文。URL 必须是有效的绝对 HTTP/HTTPS URL，非法输入不调用节点。每页最多 8000 字符；超过一页时返回
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

当前源码和 v2.10 使用 `logs/mcp_search_<PID>.log`，每个进程
写入自己的日志，记录节点调用、失败原因与熔断跳过；v2.8 起还记录节点成功/失败耗时、整次调用耗时、尝试与跳过数量和原因，空结果单独记录。每个进程的单个日志文件
上限为 2,000,000 字节，保留 3 个备份。历史 v2.0 和 v2.1 仍使用
`logs/mcp_search.log`；这些旧版同时运行多个实例时应使用不同安装目录，
或升级到 v2.10，避免共享日志轮转冲突。日志目录创建失败时回退到系统临时目录下的 `qianxv-search-mcp-logs/`；
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
Expand-Archive .\release\search-mcp-v2.10.zip .\release\_test\v2.10-smoke
$env:MCP_TEST_EXE = (Resolve-Path .\release\_test\v2.10-smoke\search-mcp-v2.10\search-mcp.exe).Path
python -m pytest tests/ --basetemp=release/_test/v2.10-pytest -q
Remove-Item Env:MCP_TEST_EXE
```

测试包含三种传输模式、端口冲突、每种模式重复启动 5 次及 3 实例并发，
以及 stdio 断开后 HTTP 存续，检查运行中和退出后的 `_MEI*` 解压残留。

真实 API 验证需要有效私有配置，只向隔离目录复制：

```powershell
Copy-Item .\config.json .\release\_test\v2.10-smoke\search-mcp-v2.10\config.json
python release/test_release.py release/_test/v2.10-smoke/search-mcp-v2.10
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
release/           v2.8/v2.9 历史发行版、v2.10 发行版及 ZIP + test_release.py
```

## 更新日志

### v2.10（2026-10-08）

- 新增无需云 API key 的本机 DDGS 搜索及 HTML 正文提取，模板在云节点后启用本机兜底。
- 本机搜索自动并发尝试启用的 DDGS text 引擎及 `bing_html`，共享节点预算并返回首个有效结果；支持指定引擎列表。
- 本机抓取沿用系统 DNS、环境代理和 TLS 校验，支持 HTTP/HTTPS/SOCKS 代理；限制页面大小与重定向次数。
- 移除 Bright Data 节点；升级前须从旧配置删除 `brightdata`，保留其他节点与凭证。
- 同步 MCP 握手版本为 2.10，更新文档及 Windows 目录发行包；保留 v2.8/v2.9，移除 v2.6/v2.7 发行包。

### v2.9（2026-10-07）

- 同步 MCP 握手版本为 2.9，沿用 v2.8 的工具签名、配置和故障转移行为。
- 更新项目自有文档、版本入口和历史验证指引，提供与当前源码对应的 Windows 目录发行包。

### v2.8（2026-10-07）

- 新增搜索与抓取总预算，默认按启用节点超时合计；限制整个节点执行过程，保留故障转移顺序。
- 搜索空结果不再熔断；至少一个节点正常响应但所有结果为空时，返回“未找到相关结果”。
- 全部节点失败或总预算耗尽时返回 MCP 工具错误（`isError: true`）；成功结果格式保持兼容。
- 熔断冷却后只允许一个恢复探测，取消及总预算截止均释放探测状态。
- 修正节点超时与总预算同时到期的归类：当前节点仍触发熔断；更短总预算提前截断时不熔断。
- 严格校验时长、布尔开关、配置对象、查询与 URL；补充节点及整次调用耗时日志。
- Windows 目录发行版新增三种传输的 mock 长文分页验证，续读不重复请求上游。


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
