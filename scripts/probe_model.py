"""探测模型服务：列出可用模型，并验证 function calling 是否可用。

用法:
    python scripts/probe_model.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(BASE_DIR / ".env")

api_key = os.getenv("LLM_API_KEY", "")
base_url = os.getenv("LLM_BASE_URL", "")
print(f"base_url = {base_url}")
print(f"api_key  = {api_key[:8]}...{api_key[-4:]} (len={len(api_key)})")

client = OpenAI(api_key=api_key, base_url=base_url, timeout=60)

try:
    models = client.models.list()
    ids = [m.id for m in models.data]
    print(f"\n可用模型 ({len(ids)} 个):")
    for i in ids:
        print(f"  - {i}")
except Exception as exc:  # noqa: BLE001
    print(f"\n[!] 模型列表接口不可用: {type(exc).__name__}: {exc}")
    ids = []

candidates = [os.getenv("LLM_MODEL")] if os.getenv("LLM_MODEL") else []
candidates = [c for c in candidates if c] or ids[:3]

for name in candidates:
    print(f"\n===== 测试模型 {name} =====")
    try:
        resp = client.chat.completions.create(
            model=name,
            messages=[{"role": "user", "content": "回复两个字：收到"}],
            max_tokens=32,
        )
        print(f"  基础对话 OK -> {resp.choices[0].message.content!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"  基础对话失败: {type(exc).__name__}: {exc}")
        continue

    tools = [{
        "type": "function",
        "function": {
            "name": "query_order",
            "description": "查询订单",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string", "description": "订单号"}},
                "required": ["order_id"],
            },
        },
    }]
    try:
        resp = client.chat.completions.create(
            model=name,
            messages=[{"role": "user", "content": "帮我查下订单 ORD20261028001"}],
            tools=tools,
            max_tokens=128,
        )
        msg = resp.choices[0].message
        if msg.tool_calls:
            tc = msg.tool_calls[0]
            print(f"  function calling OK -> {tc.function.name}({tc.function.arguments})")
        else:
            print(f"  [!] 未触发工具调用，返回: {msg.content!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"  function calling 失败: {type(exc).__name__}: {exc}")

print("\n完成。把确认可用的模型名填进 .env 的 LLM_MODEL。")
