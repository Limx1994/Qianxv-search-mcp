---
name: 创建并测试 search-mcp Release
overview: 将本地搜索 MCP 服务打包为可在其他 Windows 电脑上直接安装使用的 release（PyInstaller exe + config.json），并在独立目录完成全套验证：ruff + pytest 单测 + stdio 真实启动验证 MCP 握手与 search/extract 工具。
todos:
  - id: patch-frozen-paths
    content: 改造 config_loader.py 与 logger.py 增加 frozen 模式路径支持（exe 旁定位 config.json 与 logs/）
    status: completed
  - id: build-release
    content: 创建 venv 装依赖，跑 ruff + pytest 回归后用 PyInstaller 构建 exe 并组装 release 目录与 zip（含 config.json 与安装说明）
    status: completed
    dependencies:
      - patch-frozen-paths
  - id: write-release-test
    content: 编写 release/test_release.py：以 stdio 子进程启动 exe 完成握手、tools/list、search/extract 真实调用断言
    status: completed
  - id: run-full-verify
    content: 解压 zip 到 release/_test 独立目录执行全套验证，确认全部通过
    status: completed
    dependencies:
      - build-release
      - write-release-test
  - id: cleanup-artifacts
    content: 清理 venv、build/dist 临时产物，保留 release zip 与测试结论
    status: completed
    dependencies:
      - run-full-verify
---

## 产品概述

为本地搜索 MCP 服务（search/extract 双工具，多搜索源故障转移）创建可在其他 Windows 电脑上直接安装使用的发行版：打包为免 Python 环境的独立 exe，附真实 config.json 开箱即用，并附带安装说明；随后对 release 做全套验证（解压到独立目录、ruff + pytest 单测回归、stdio 真实启动验证 MCP 握手与两个工具的真实调用）。

## 核心功能

- **Release 打包**：用 PyInstaller 将 `server.py` 打包为单文件 `search-mcp.exe`，目标机器无需安装 Python
- **配置随包**：release 内附当前真实 `config.json`（含全部密钥），exe 同目录自动加载
- **安装说明**：`安装说明.md` 提供 MCP 客户端 `mcpServers` 接入片段与配置替换指引
- **路径兼容改造**：`config_loader.py` / `logger.py` 支持 PyInstaller frozen 模式，exe 旁读写 config.json 与 logs/
- **全套测试**：独立目录解压 zip → venv 装依赖 → ruff + pytest → stdio 真实启动 exe，完成 initialize 握手、tools/list、真实 search/extract 调用验证

## 技术栈

- 现有项目栈：Python 3.14 + mcp>=2.0.0 + httpx（stdio 传输），pytest + ruff 质量门禁
- 新增打包工具：PyInstaller（`--onefile` 模式）
- 测试客户端：复用 mcp 库自带 `ClientSession` + `stdio_client`，保证与 server 协议版本完全一致
- 脚本环境：Windows PowerShell

## 实现方案

### 关键问题与决策

1. **Frozen 路径兼容（本方案核心改动）**：

- `config_loader.py:10` 的 `CONFIG_PATH = Path(__file__).resolve().parent / "config.json"`，在 PyInstaller onefile 下 `__file__` 指向临时解压目录 `_MEIPASS`，exe 旁的 config.json 将无法加载（且 `server.py` import 时即调用 `load_config()`，会直接启动失败）
- `logger.py:9` 的 `LOG_DIR` 同理，frozen 下日志会写进临时目录并随进程退出丢失
- **方案**：在两处增加 frozen 判断——`getattr(sys, "frozen", False)` 时基于 `Path(sys.executable).parent` 定位 config.json 与 logs/ 目录；源码运行行为保持完全不变（向后兼容，`load_config(path)` 与 `setup_logging` 签名不变，不影响现有 tests/）

2. **providers 收集**：`providers/__init__.py` 全部为静态 import，PyInstaller 依赖分析可自动覆盖，无需 `--collect-submodules`；mcp/httpx 的动态导入若打包后启动报 `ModuleNotFoundError`，用 `--hidden-import` 补齐
3. **Release 组成**：`release/search-mcp-v1.0/` 目录 → `search-mcp.exe` + `config.json`（真实密钥，用户已确认）+ `安装说明.md`；压缩为 `release/search-mcp-v1.0.zip`
4. **真实测试**：编写 `release/test_release.py`，以子进程 stdio 启动解压后的 exe，依次执行 initialize 握手 → `tools/list`（断言含 search/extract）→ `tools/call search`（真实查询，断言含"来源节点"与结果条目）→ `tools/call extract`（真实 URL，断言标题/正文非空）；走真实 config 与外部 API，完整覆盖故障转移链路

### 架构（构建与验证流程）

```mermaid
flowchart LR
    A[改造 config_loader.py / logger.py<br/>frozen 路径支持] --> B[venv 安装依赖<br/>ruff + pytest 回归]
    B --> C[PyInstaller 打包 onefile exe]
    C --> D[组装 release 目录<br/>exe + config.json + 安装说明]
    D --> E[zip 压缩到 release/]
    E --> F[解压到独立测试目录<br/>stdio 真实启动全链路验证]
    F --> G[清理 venv / build 临时产物]
```

### 目录结构（变更清单）

```
d:/搜索MCP/
├── config_loader.py        # [MODIFY] 新增 _app_dir() 帮助函数：frozen 时取 sys.executable 所在目录，
│                          #   CONFIG_PATH 基于该目录定位；load_config 签名不变
├── logger.py              # [MODIFY] LOG_DIR 同样基于 frozen 判断定位到 exe 旁的 logs/；源码模式不变
├── server.py              # [不动] 入口与工具注册保持现状
├── release/               # 发行产物目录（已存在，当前为空）
│   ├── build_release.ps1  # [NEW] 一键构建脚本：venv → 质检 → PyInstaller → 组装目录 → zip
│   ├── test_release.py    # [NEW] stdio 真实验证脚本：握手/tools list/search/extract 断言
│   ├── search-mcp-v1.0/   # [NEW] 解压态目录：search-mcp.exe + config.json + 安装说明.md
│   ├── search-mcp-v1.0.zip# [NEW] 最终发行包
│   └── _test/             # [NEW-临时] 全套验证用的独立解压目录，验证后清理
└── 安装说明.md（位于包内） # [NEW] mcpServers 配置片段、config.json 更换说明、日志位置说明
```

### 关键代码结构（frozen 路径判定，两处共用模式）

```python
import sys
from pathlib import Path

def _app_dir() -> Path:
    """应用基准目录：PyInstaller frozen 时为 exe 所在目录，否则为源码目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

CONFIG_PATH = _app_dir() / "config.json"  # config_loader.py
LOG_DIR = _app_dir() / "logs"             # logger.py
```

## 实施要点

- **构建产物体积与启动**：onefile exe 首启需自解压到临时目录（约 1~2 秒），MCP 客户端启动超时建议在安装说明中提示设为 15 秒以上
- **blast radius**：`config_loader.py`/`logger.py` 改动仅新增 frozen 分支，源码运行路径逐字节不变；现有 4 个测试文件（纯 mock）必须全绿后才进入打包
- **测试隔离**：真实 stdio 验证在 `release/_test/` 独立目录解压运行，验证日志落在该目录 `logs/`，不污染源码目录；`.pyc` 缓存文件不打入 release
- **敏感信息**：config.json 含真实密钥，仅进入本地 zip（项目非 git 仓库，无提交泄漏风险）；安装说明中明示密钥文件请妥善保管
- **日志脱敏**：沿用 `logger.py` 现有 `mask_key` 机制，验证时不检查日志中的完整密钥出现
- **清理**：构建 venv、`build/`、`dist/`、`*.spec` 在验证通过后删除，仅保留 zip 与解压态目录