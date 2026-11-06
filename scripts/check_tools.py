"""检查 MCP Server 通过协议暴露出的工具契约。

用法:
    python scripts/check_tools.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

load_dotenv(BASE_DIR / ".env")

from agent.tools_bridge import load_tools  # noqa: E402


async def main() -> None:
    tools = await load_tools()
    print(f"发现 {len(tools)} 个工具\n")
    for tool in tools:
        print("=" * 70)
        print(f"name: {tool.name}")
        schema = tool.args_schema
        if not isinstance(schema, dict):
            schema = schema.model_json_schema()
        required = schema.get("required", [])
        print(f"required params: {required or '（无）'}")
        print("parameters:")
        for key, meta in schema.get("properties", {}).items():
            flags = []
            if key in required:
                flags.append("必填")
            if "enum" in meta:
                flags.append("枚举")
            if "maximum" in meta or "minimum" in meta:
                flags.append(f"{meta.get('minimum')}~{meta.get('maximum')}")
            tag = f" [{'/'.join(flags)}]" if flags else " [可选]"
            print(f"  - {key}: {meta.get('type', meta.get('anyOf'))}{tag}")
            print(f"      {meta.get('description', '')}")
        print(f"annotations: readOnly={tool.metadata}")
        desc = tool.description or ""
        print(f"description ({len(desc)} 字):")
        print("  " + desc.replace("\n", "\n  "))
        print()

    print("=" * 70)
    print("工具调用连通性测试")
    result = await tools[0].ainvoke({"limit": 3})
    print(json.dumps(result, ensure_ascii=False, indent=2)[:900])


if __name__ == "__main__":
    asyncio.run(main())
