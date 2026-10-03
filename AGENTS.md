# 项目开发指南

## 项目结构与模块

`server.py` 提供 stdio、Streamable HTTP、`both` 传输及 `search`、`extract` 工具；`search_router.py` 负责故障转移和熔断，`config_loader.py` 校验节点配置，`logger.py` 管理日志。`providers/` 存放各搜索、抓取服务的适配器及基类；新增节点时按现有注册表接入。`tests/` 包含 mock 单元测试与进程级传输测试；`release/` 存放 Windows 发行版和 `test_release.py`。运行日志位于 `logs/`。

## 安装、运行与验证

项目要求 Python 3.10+，优先使用 PowerShell。在仓库根目录运行；首次启动前按 README 从无密钥模板创建并配置 `config.json`：

```powershell
python -m pip install -r requirements.txt  # 安装运行及开发依赖
python server.py                 # 启动 stdio MCP 服务
python server.py --transport streamable-http  # 启动本机 HTTP 服务
ruff check .                     # 检查 Python 代码
python -m pytest tests/          # 运行单元和传输测试
```

Windows 发行版使用现有 `search-mcp.spec`，安装 PyInstaller 后运行 `python -m PyInstaller --noconfirm search-mcp.spec`。产物位于 `dist\search-mcp\`，包含 exe 和 `_internal\`；组装发行包时添加无密钥的 `config.example.json` 和对应安装说明。

发行版冒烟测试使用 `MCP_TEST_EXE` 指向待测 exe，要求其同目录存在 `_internal\` 和 `config.example.json`。测试自动复制依赖并生成禁用节点的隔离配置；`--basetemp` 必须指定项目内 `release\_test\` 下的独立测试目录：

```powershell
$env:MCP_TEST_EXE = (Resolve-Path .\release\search-mcp-v2.5\search-mcp.exe).Path
python -m pytest tests/ --basetemp=release/_test/v2.5-pytest -q
Remove-Item Env:MCP_TEST_EXE
```

`python release/test_release.py <发行版目录>` 检查 MCP 握手、工具列表、真实 `search` / `extract` 调用和日志落地。仅在有效配置可用时运行；验证目录放在 `release\_test\`，私有 `config.json` 不得写入公开发行目录。该脚本会请求外部 API，失败必须单独报告。

## 代码风格与命名

沿用现有 Python 风格：四空格缩进、模块与函数使用 `snake_case`、类使用 `PascalCase`，公开接口添加类型标注，失败通过现有异常类型明确上报。函数名和变量名不超过 40 个字符，单文件原则上不超过 4800 行。使用 `ruff check .` 做 lint；仓库没有独立格式化配置。复用 `providers/base.py`、`providers/extract_base.py` 的基类与 HTTP 辅助方法，只修改当前任务必需内容。

## 测试要求

使用 `pytest`；测试文件采用 `tests/test_*.py`，测试函数采用 `test_*`。为新增或修改的节点行为补充 mock 测试，覆盖成功结果、错误和故障转移。传输变化还需验证 stdio、HTTP 和 `both`、端口冲突及 stdio 断开后的 HTTP 行为。目录发行版还需验证重复启动、并发运行和 `_MEI*` 残留。仓库未规定数值覆盖率门槛。

提交前运行 lint、全部单元测试和发行版冒烟测试，记录通过、失败、跳过数量及原因。未设置 `MCP_TEST_EXE` 时发行版测试会跳过，不能据此宣称发行版验证通过。测试和日志仅作验证依据，禁止将未验证结果写成成功。

## 提交与 Pull Request

现有提交数量较少，尚无固定格式；使用简短、能说明变更的主题，例如 `rename: ...` 或 `v1.0: ...`。Pull Request 应说明改动范围、验证命令与结果；涉及行为变化时附复现步骤，关联适用的 issue。仅在界面或可见输出发生变化时附截图或示例输出。

## 配置与密钥

`config.json` 和 `说明.txt` 含敏感信息，已列入 `.gitignore`；不要提交私有配置、日志、测试备份或真实凭证。v2.0、v2.1 和 v2.5 发行包仅分发无密钥的 `config.example.json`，用户自行复制并配置；第三方依赖中的许可证文件保持原文。检查目录与 ZIP 内容一致，升级时保留用户的 `config.json` 和 `logs\`。

`nodes` 与 `extract_nodes` 的排列顺序决定故障转移顺序，注册表顺序不是调用顺序。`extract_nodes` 可省略；提供时须为非空数组。调整节点时同时检查类型校验、注册表及对应测试。
