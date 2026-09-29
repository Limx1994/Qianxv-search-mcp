"""Release 全链路验证脚本：以 stdio 子进程启动解压后的 exe，
完成 MCP 握手、tools/list、search/extract 真实调用断言。

用法（在构建 venv 或装有 mcp 库的 Python 下）：

    python test_release.py <release目录路径>

例如：
    python test_release.py release/_test/search-mcp-v1.0

会真实调用外部搜索 / 抓取 API（走包内 config.json）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SEARCH_QUERY = "大模型 MCP 协议是什么"
EXTRACT_URL = "https://modelcontextprotocol.io/introduction"

_passed = 0
_failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录并打印单条断言结果。"""
    global _passed
    mark = "PASS" if ok else "FAIL"
    suffix = f"  {detail}" if detail else ""
    print(f"[{mark}] {name}{suffix}", flush=True)
    if ok:
        _passed += 1
    else:
        _failed.append(name)


async def run(exe_dir: Path) -> int:
    exe = exe_dir / "search-mcp.exe"
    check("exe 存在", exe.is_file(), str(exe))
    check("config.json 存在", (exe_dir / "config.json").is_file())
    if not exe.is_file():
        return 1

    server = StdioServerParameters(
        command=str(exe),
        args=[],
        cwd=str(exe_dir),
    )
    async with (
        stdio_client(server) as (read, write),
        ClientSession(read, write) as session,
    ):
            # 1. MCP initialize 握手
            init = await asyncio.wait_for(session.initialize(), timeout=30)
            server_name = init.server_info.name
            check(
                "MCP 握手",
                server_name == "search-mcp",
                f"server={server_name} protocol={init.protocol_version}",
            )

            # 2. tools/list：应包含 search 与 extract
            tools = await asyncio.wait_for(session.list_tools(), timeout=15)
            names = {t.name for t in tools.tools}
            check("tools/list 包含 search", "search" in names, str(sorted(names)))
            check("tools/list 包含 extract", "extract" in names)

            # 3. 真实调用 search
            try:
                res = await asyncio.wait_for(
                    session.call_tool(
                        "search", {"query": SEARCH_QUERY, "max_results": 3}
                    ),
                    timeout=90,
                )
                text = res.content[0].text if res.content else ""
                check(
                    "search 调用成功",
                    not res.is_error and "来源节点" in text and "1." in text,
                    text.splitlines()[0] if text else "(空结果)",
                )
            except Exception as exc:  # noqa: BLE001
                check("search 调用成功", False, repr(exc))

            # 4. 真实调用 extract
            try:
                res = await asyncio.wait_for(
                    session.call_tool("extract", {"url": EXTRACT_URL}),
                    timeout=90,
                )
                text = res.content[0].text if res.content else ""
                body = text.split("---", 1)[-1].strip()
                check(
                    "extract 调用成功",
                    not res.is_error
                    and "来源节点" in text
                    and "标题" in text
                    and len(body) > 50,
                    f"正文 {len(body)} 字符",
                )
            except Exception as exc:  # noqa: BLE001
                check("extract 调用成功", False, repr(exc))

    # 5. 日志落地到 exe 旁 logs/
    log = exe_dir / "logs" / "mcp_search.log"
    check("日志写入 exe 旁 logs/", log.is_file(), str(log))

    print(f"\n共 {_passed + len(_failed)} 项：通过 {_passed}，"
          f"失败 {len(_failed)}")
    return 1 if _failed else 0


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    exe_dir = Path(sys.argv[1]).resolve()
    if not exe_dir.is_dir():
        print(f"目录不存在: {exe_dir}")
        sys.exit(2)
    sys.exit(asyncio.run(run(exe_dir)))


if __name__ == "__main__":
    main()
