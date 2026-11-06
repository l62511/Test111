"""订单客服 Agent —— ReAct 命令行入口。

用法:
    python -m agent.main
    python -m agent.main --verbose      # 打印工具返回原文
    python -m agent.main --as u_1001    # 跳过登录，以指定身份进入（调试）

说明：
- 取消订单是终态操作，挂了 HumanInTheLoopMiddleware，模型发起调用后会先停下，
  由终端里的人确认。
- 提交退款不拦：金额与通道由规则引擎决定，提交本身不是不可逆的放款动作。
- 每一轮的思考、工具调用与结果都会写入审计事件流，网页端可回看。
"""

from __future__ import annotations

import asyncio
import getpass
import sys
from datetime import datetime
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
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from agent.llm import build_llm  # noqa: E402
from agent.prompt import build_system_prompt  # noqa: E402
from agent.runner import INTERRUPT_ON, ConversationRunner  # noqa: E402
from agent.tools_bridge import mcp_tools  # noqa: E402
from mcp_server import audit, service  # noqa: E402

VERBOSE = "--verbose" in sys.argv or "-v" in sys.argv


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


async def _ask_approval(requests, lines) -> Command:
    """把中断里的待执行动作显示出来，问用户放不放行。"""
    print("\n   ---------- 需要你确认 ----------")
    for line in lines:
        print(f"   {line}")
    print("   --------------------------------")
    try:
        answer = input("   确认执行？输入 y 执行，其它任意键取消 > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    if answer in {"y", "yes", "是", "确认", "通过"}:
        print("   [已确认]")
        return Command(resume={"decisions": [{"type": "approve"} for _ in requests]})
    print("   [已取消操作]")
    return Command(resume={"decisions": [
        {"type": "reject",
         "message": "用户在当前终端取消了该操作，请如实告知用户操作未执行。"}
        for _ in requests
    ]})


async def run() -> None:
    as_user = _arg_value("--as")
    if as_user:
        try:
            caller = service.resolve_caller(as_user)
        except service.BizError as exc:
            raise SystemExit(f"身份校验失败: {exc.message}") from exc
    else:
        user = login_interactive()
        caller = {"caller_id": user["user_id"], "caller_name": user["name"]}
    caller_id = caller["caller_id"]

    session_id = f"cli-{caller_id}-{datetime.now():%Y%m%d%H%M%S}"
    audit.ensure_session(session_id, caller_id, caller["caller_name"], audit.CHANNEL_CLI)

    print("=" * 62)
    print("  订单客服 Agent  |  ReAct + MCP")
    print(f"  当前用户: {caller['caller_name']} ({caller_id})")
    print(f"  会话编号: {session_id}")
    print(f"  数据目录: {service.store.data_dir()}")
    print("  输入 exit 退出，help 查看示例问题")
    print("=" * 62)

    async with mcp_tools(caller_id) as tools:
        print(f"  已加载工具: {', '.join(t.name for t in tools)}")
        print("  退款金额与是否需人工由系统规则决定；取消订单需你确认\n")

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
        runner = ConversationRunner(
            agent,
            {"configurable": {"thread_id": session_id}},
            caller,
            channel=audit.CHANNEL_CLI,
            session_id=session_id,
            verbose=VERBOSE,
            echo=print,
        )

        examples = (
            "试试这样问：\n"
            "  我最近有哪些订单？\n"
            "  我这单退款到哪一步了？ORD20261010007\n"
            "  ORD20261102005 键盘键帽松动，帮我退款\n"
            "  帮我把 ORD20261015002 取消掉\n"
            "  本周退款金额最高的商品是什么？"
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
                result = await runner.send(text, _ask_approval)
                print(f"\n小柚 > {result.reply or '（本轮没有生成回复，请重试）'}")
            except Exception as exc:  # noqa: BLE001
                print(f"\n[出错] {type(exc).__name__}: {exc}")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n再见。")


if __name__ == "__main__":
    main()
