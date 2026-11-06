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
import getpass
import json
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
from langchain.agents.middleware import HumanInTheLoopMiddleware  # noqa: E402
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from agent.llm import build_llm  # noqa: E402
from agent.prompt import build_system_prompt  # noqa: E402
from agent.tools_bridge import mcp_tools  # noqa: E402
from mcp_server import service  # noqa: E402

VERBOSE = "--verbose" in sys.argv or "-v" in sys.argv

# 提交退款申请不是不可逆操作（钱要等商家审批才动），所以不拦。
# 取消订单是终态操作，仍然需要在终端确认一次。
INTERRUPT_ON = {
    "cancel_order": True,
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
    if name == "cancel_order" and args.get("order_id"):
        try:
            info = service.query_orders(caller_id, order_id=args["order_id"])["orders"][0]
            lines.append(f"   订单    : {info['item_name']}  {info['amount']}")
            lines.append(f"   当前状态: {info['status_label']}")
        except Exception:  # noqa: BLE001
            pass
    return lines


def _ask_approval(value: dict, caller: dict) -> Command:
    """把中断里的待执行动作显示出来，问用户放不放行。"""
    requests = value.get("action_requests", []) or []
    print("\n   ---------- 需要你确认 ----------")
    for req in requests:
        for line in _describe(req, caller["caller_id"]):
            print(line)
    print("   --------------------------------")

    try:
        answer = input("   确认执行？输入 y 执行，其它任意键取消 > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"

    if answer in {"y", "yes", "是", "确认", "通过"}:
        print("   [已确认]")
        decisions = [{"type": "approve"} for _ in requests]
    else:
        print("   [已取消操作]")
        decisions = [
            {"type": "reject", "message": "用户在当前终端取消了该操作，请如实告知用户操作未执行。"}
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


def _arg_value(flag: str) -> str | None:
    if flag in sys.argv:
        idx = sys.argv.index(flag)
        if idx + 1 < len(sys.argv):
            return sys.argv[idx + 1]
    return None


def login_interactive() -> dict:
    """账号密码登录。三次失败即退出。"""
    print("  请登录（演示账号：zhangwei / 123456）")
    for attempt in range(3):
        try:
            username = input("  账号 > ").strip()
            password = getpass.getpass("  密码 > ")
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\n已取消登录") from None
        try:
            return service.authenticate(username, password, role="customer")
        except service.BizError as exc:
            left = 2 - attempt
            if left > 0:
                print(f"  {exc.message}，还有 {left} 次机会")
            else:
                print(f"  {exc.message}")
    raise SystemExit("登录失败次数过多，已退出")


async def run() -> None:
    as_user = _arg_value("--as")
    if as_user:
        # 调试通道：跳过登录，直接以指定身份进入
        try:
            caller = service.resolve_caller(as_user)
        except service.BizError as exc:
            raise SystemExit(f"身份校验失败: {exc.message}") from exc
    else:
        user = login_interactive()
        caller = {"caller_id": user["user_id"], "caller_name": user["name"]}
    caller_id = caller["caller_id"]

    print("=" * 62)
    print("  订单客服 Agent  |  ReAct + MCP")
    print(f"  当前用户: {caller['caller_name']} ({caller['caller_id']})")
    print(f"  数据目录: {service.store.data_dir()}")
    print("  输入 exit 退出，help 查看示例问题")
    print("=" * 62)

    # 整个对话复用一条 MCP 连接，避免每次工具调用都重启子进程
    async with mcp_tools(caller_id) as tools:
        print(f"  已加载工具: {', '.join(t.name for t in tools)}")
        print("  未发货直接退；已发货/已签收提交申请，等商家后台审批；取消需确认\n")

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
