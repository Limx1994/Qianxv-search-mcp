# Repository Guidelines

## 项目结构与模块

`server.py` 提供 stdio MCP 入口及 `search`、`extract` 工具；`search_router.py` 负责故障转移和熔断，`config_loader.py` 校验节点配置，`logger.py` 管理日志。`providers/` 存放各搜索、抓取服务的适配器及基类；新增节点时按现有注册表接入。`tests/` 是使用 mock 的单元测试；`release/` 存放 Windows 发行版和 `test_release.py`。运行日志位于 `logs/`。

## 安装、运行与验证

项目要求 Python 3.10+，目前没有单独的构建脚本。在仓库根目录运行：

```powershell
pip install -r requirements.txt  # 安装运行及开发依赖
python server.py                 # 启动 stdio MCP 服务
ruff check .                     # 检查 Python 代码
python -m pytest tests/          # 运行 mock 单元测试
```

`python release/test_release.py release/search-mcp-v1.0` 检查已打包程序的 MCP 握手及工具调用；它会请求真实外部 API，仅在需要验证发行版且配置可用时运行。

## 代码风格与命名

沿用现有 Python 风格：四空格缩进、模块与函数使用 `snake_case`、类使用 `PascalCase`，公开接口添加类型标注，失败通过现有异常类型明确上报。使用 `ruff check .` 做 lint；仓库没有独立格式化配置。复用 `providers/base.py`、`providers/extract_base.py` 的基类与 HTTP 辅助方法。

## 测试要求

使用 `pytest`；测试文件采用 `tests/test_*.py`，测试函数采用 `test_*`。为新增或修改的节点行为补充 mock 测试，覆盖成功结果、错误和故障转移。仓库未规定数值覆盖率门槛。提交前运行 lint 和全部单元测试。

## 提交与 Pull Request

现有提交数量较少，尚无固定格式；使用简短、能说明变更的主题，例如 `rename: ...` 或 `v1.0: ...`。Pull Request 应说明改动范围、验证命令与结果；涉及行为变化时附复现步骤，关联适用的 issue。仅在界面或可见输出发生变化时附截图或示例输出。

## 配置与密钥

`config.json` 含 API Key，已列入 `.gitignore`；不要提交它、发行包内的配置、日志或真实凭证。`nodes` 与 `extract_nodes` 的排列顺序决定故障转移顺序；调整节点时同时检查类型校验、注册表及对应测试。
