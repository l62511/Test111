"""订单客服 Agent —— ReAct 命令行入口。

用法:
    python -m agent.main
    python -m agent.main --verbose      # 打印工具返回原文

说明：
- 取消 / 退款是写操作，挂了 HumanInTheLoopMiddleware，
  模型发起调用后会在真正执行前停下来，由终端里的人确认。
- 查询是只读，直接执行，不打断对话。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

load_dotenv(BASE_DIR / ".env")

from langchain.agents import create_agent  # noqa: E402
from langchain.agents.middleware import (  # noqa: E402
    HumanInTheLoopMiddleware,
    InterruptOnConfig,
)
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from agent.llm import build_llm  # noqa: E402
from agent.prompt import build_system_prompt  # noqa: E402
from agent.tools_bridge import mcp_tools  # noqa: E402
from mcp_server import service  # noqa: E402

VERBOSE = "--verbose" in sys.argv or "-v" in sys.argv


def _refund_needs_approval(req) -> bool:
    """只有带上 approval_id 的退款调用才中断等审批。

    第一次调用（不带 approval_id）只是「开审批单」，未发货的订单会在这一步
    直接退款成功，不该弹确认框；已发货的订单会在服务层生成审批单并返回
    MANUAL_APPROVAL_REQUIRED，模型带上 approval_id 重试时才据此打断。
    """
    call = getattr(req, "tool_call", None) or {}
    args = call.get("args") or {}
    return bool(args.get("approval_id"))


INTERRUPT_ON = {
    "cancel_order": True,  # 取消始终确认
    "refund_order": InterruptOnConfig(
        allowed_decisions=["approve", "reject"],
        description="已发货订单退款，需要人工审批",
        when=_refund_needs_approval,
    ),
}


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(content or "")


def _preview(payload, limit: int = 160) -> str:
    if not isinstance(payload, str):
        payload = json.dumps(payload, ensure_ascii=False, default=str)
    payload = " ".join(payload.split())
    return payload if len(payload) <= limit else payload[:limit] + "..."


def _print_step(msg: BaseMessage) -> None:
    if isinstance(msg, AIMessage) and msg.tool_calls:
        for call in msg.tool_calls:
            args = json.dumps(call.get("args", {}), ensure_ascii=False, default=str)
            print(f"   调用 {call['name']} {args}")
    elif isinstance(msg, ToolMessage):
        name = getattr(msg, "name", "tool")
        body = _text(msg.content)
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            data = None
        if isinstance(data, dict) and data.get("ok") is False:
            print(f"   结果 {name}: 拒绝 —— {data.get('message')}")
        else:
            summary = data.get("summary") if isinstance(data, dict) else None
            print(f"   结果 {name}: {summary or _preview(body)}")
        if VERBOSE:
            print(f"   raw: {_preview(body, 600)}")


async def _drive(agent, payload, config, seen: int) -> tuple[dict | None, int]:
    """跑一轮，边跑边打印新增消息，返回最终状态与已读消息数。"""
    final = None
    async for state in agent.astream(payload, config, stream_mode="values"):
        final = state
        for msg in state["messages"][seen:]:
            _print_step(msg)
        seen = len(state["messages"])
    return final, seen


def _describe(req: dict, caller_id: str) -> list[str]:
    name = req.get("name", "?")
    args = req.get("args") or {}
    lines = [
        f"   即将执行: {name}",
        f"   参数    : {json.dumps(args, ensure_ascii=False, default=str)}",
    ]
    if name == "refund_order" and args.get("order_id"):
        try:
            info = service.query_orders(caller_id, order_id=args["order_id"])["orders"][0]
            shipped = info["shipped"]
            lines.append(f"   订单    : {info['item_name']}  {info['amount']}")
            lines.append(
                f"   发货状态: {'已发货 ' + (info['shipped_at'] or '') if shipped else '未发货'}"
                + (f"  运单 {info['tracking_no']}" if info.get("tracking_no") else "")
            )
            lines.append(f"   退款原因: {args.get('reason') or '（未填写）'}")
        except Exception:  # noqa: BLE001
            pass
    return lines


def _ask_approval(value: dict, caller: dict) -> Command:
    """把中断里的待执行动作显示出来，问用户放不放行。"""
    requests = value.get("action_requests", []) or []
    print("\n   ---------- 需要人工审批 ----------")
    for req in requests:
        for line in _describe(req, caller["caller_id"]):
            print(line)
    print("   ----------------------------------")

    try:
        answer = input("   确认通过审批？输入 y 通过，其它任意键驳回 > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"

    approved = answer in {"y", "yes", "是", "确认", "通过"}

    if approved:
        print("   [审批通过]")
        decisions = [{"type": "approve"} for _ in requests]
    else:
        print("   [审批驳回]")
        # 驳回要落进 trace，否则追踪链里会留一条永远 PENDING 的单子
        for req in requests:
            approval_id = (req.get("args") or {}).get("approval_id")
            if approval_id:
                try:
                    service.reject_refund(
                        approval_id,
                        approver=f"{caller['caller_name']}（终端人工驳回）",
                        note="终端审批未通过",
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"   [警告] 驳回状态未写入追踪记录: {exc}")
        decisions = [
            {"type": "reject", "message": "人工审批未通过，请如实告知用户该退款没有执行，不要重试。"}
            for _ in requests
        ]
    return Command(resume={"decisions": decisions})


async def _run_turn(agent, config, payload, seen: int, caller: dict) -> tuple[dict | None, int]:
    """驱动一轮对话，遇到需要审批的写操作就询问，直到模型给出最终回复。"""
    final, seen = await _drive(agent, payload, config, seen)

    for _ in range(5):  # 防御：极端情况下中断反复出现
        snapshot = await agent.aget_state(config)
        pending = getattr(snapshot, "interrupts", ()) or ()
        if not pending:
            break
        final, seen = await _drive(agent, _ask_approval(pending[0].value, caller), config, seen)

    return final, seen


async def run() -> None:
    caller_id = os.getenv("CALLER_ID", "").strip()
    if not caller_id:
        raise SystemExit("CALLER_ID 未配置，请先编辑 .env")

    try:
        caller = service.resolve_caller(caller_id)
    except service.BizError as exc:
        raise SystemExit(f"身份校验失败: {exc.message}") from exc

    print("=" * 62)
    print("  订单客服 Agent  |  ReAct + MCP")
    print(f"  当前用户: {caller['caller_name']} ({caller['caller_id']})")
    print(f"  数据库  : {service.db_path()}")
    print("  输入 exit 退出，help 查看示例问题")
    print("=" * 62)

    # 整个对话复用一条 MCP 连接，避免每次工具调用都重启子进程
    async with mcp_tools(caller_id) as tools:
        print(f"  已加载工具: {', '.join(t.name for t in tools)}")
        print("  已发货订单退款需人工审批；未发货直接退款；取消需确认\n")

        agent = create_agent(
            model=build_llm(),
            tools=tools,
            system_prompt=build_system_prompt(caller["caller_name"]),
            middleware=[
                HumanInTheLoopMiddleware(
                    interrupt_on=INTERRUPT_ON,
                    description_prefix="不可逆的订单写操作",
                )
            ],
            checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": f"cli-{caller_id}"}}
        seen = 0

        examples = (
            "试试这样问：\n"
            "  我最近有哪些订单？\n"
            "  帮我把 ORD20261028001 取消掉\n"
            "  ORD20261015002 退款\n"
            "  帮我查一下客户李娜的订单"
        )

        while True:
            try:
                text = input("\n你 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见。")
                return

            if not text:
                continue
            if text.lower() in {"exit", "quit", "q"}:
                print("再见。")
                return
            if text.lower() in {"help", "?"}:
                print(examples)
                continue

            try:
                final, seen = await _run_turn(
                    agent, config, {"messages": [("user", text)]}, seen, caller
                )
                reply = _text(final["messages"][-1].content).strip()
                print(f"\n小柚 > {reply or '（本轮没有生成回复，请重试）'}")
            except Exception as exc:  # noqa: BLE001
                print(f"\n[出错] {type(exc).__name__}: {exc}")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n再见。")


if __name__ == "__main__":
    main()
