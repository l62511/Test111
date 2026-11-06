"""把 MCP Server 暴露的工具桥接成 LangChain Tool。

身份在这里通过子进程环境变量注入，不经过模型，也不出现在任何工具 schema 里。
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

BASE_DIR = Path(__file__).resolve().parent.parent


def build_client(caller_id: str | None = None) -> MultiServerMCPClient:
    caller_id = caller_id or os.getenv("CALLER_ID", "").strip()
    if not caller_id:
        raise RuntimeError("CALLER_ID 未配置，无法以已认证身份启动 MCP Server")

    data_dir = os.getenv("ORDER_DATA_DIR") or str(BASE_DIR / "data")
    if not Path(data_dir).is_absolute():
        data_dir = str(BASE_DIR / data_dir)

    env = {
        **os.environ,
        "CALLER_ID": caller_id,
        "ORDER_DATA_DIR": data_dir,
        "PYTHONPATH": str(BASE_DIR),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }

    return MultiServerMCPClient(
        {
            "order": {
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-m", "mcp_server.server"],
                "env": env,
                "cwd": str(BASE_DIR),
            }
        }
    )


@asynccontextmanager
async def mcp_tools(caller_id: str | None = None) -> AsyncIterator[list[BaseTool]]:
    """在整个会话生命周期内复用同一条 MCP 连接。

    `get_tools()` 是无状态的：每次工具调用都会重新拉起 MCP 子进程（约 0.3 秒）。
    这里持有一个 ClientSession，一次对话只启动一次子进程，工具调用直接复用该连接。
    """
    client = build_client(caller_id)
    async with client.session("order") as session:
        tools = await load_mcp_tools(session)
        yield list(tools)


async def load_tools() -> list[BaseTool]:
    """一次性取工具清单后立即断连，供检查脚本使用。"""
    tools = await build_client().get_tools()
    return list(tools)
