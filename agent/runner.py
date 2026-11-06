"""一轮对话的驱动器：跑 ReAct、采集步骤、写审计。

CLI 与网页会话页共用这里，区别只在"谁来批准写操作"：

- CLI 传一个用 input() 询问的 approver；
- 网页传一个把待确认动作留在会话里、抛 PendingApproval 的 approver，
  等用户点了按钮再带着决定重新进来。

这样"跑一轮"只有一份实现，不会出现命令行能退款、网页却退不了这类漂移。

步骤采集按消息顺序还原：模型输出的文本算思考，带 tool_calls 的算调用，
ToolMessage 算结果。最终回复单独标注，避免把中间那句话当成结论。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.types import Command

from mcp_server import audit, service

# 写操作需要终端/页面确认
INTERRUPT_ON = {"cancel_order": True}

Step = dict[str, Any]


class PendingApproval(Exception):
    """网页端无法当场确认时抛出，把待办留给下一次请求。"""

    def __init__(self, requests: list[dict[str, Any]], lines: list[str]) -> None:
        super().__init__("需要用户确认")
        self.requests = requests
        self.lines = lines


def text_of(content: Any) -> str:
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


def preview(payload: Any, limit: int = 220) -> str:
    if not isinstance(payload, str):
        payload = json.dumps(payload, ensure_ascii=False, default=str)
    payload = " ".join(payload.split())
    return payload if len(payload) <= limit else payload[:limit] + "…"


def _result_step(msg: ToolMessage) -> Step:
    body = text_of(msg.content)
    name = getattr(msg, "name", None) or "tool"
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        data = None
    if isinstance(data, dict) and data.get("ok") is False:
        return {
            "type": "tool_result", "name": name, "status": "ERROR",
            "text": data.get("message") or preview(body),
            "code": data.get("error_code"),
            "summary": preview(body, 400),
            "raw": preview(body, 1200),
        }
    summary = None
    if isinstance(data, dict):
        summary = data.get("summary") or data.get("message")
    return {
        "type": "tool_result", "name": name, "status": "OK",
        "text": summary or preview(body),
        "summary": preview(body, 400),
        "raw": preview(body, 1200),
    }


def collect_steps(messages: list[BaseMessage]) -> list[Step]:
    """把新增消息按顺序摊成步骤列表。最后一条有内容的 AIMessage 是结论。"""
    steps: list[Step] = []
    for msg in messages:
        if isinstance(msg, AIMessage):
            body = text_of(msg.content).strip()
            calls = getattr(msg, "tool_calls", None) or []
            if body:
                steps.append({"type": "thought", "text": body})
            for call in calls:
                steps.append({
                    "type": "tool_call",
                    "name": call.get("name"),
                    "args": call.get("args", {}),
                    "text": json.dumps(call.get("args", {}), ensure_ascii=False, default=str),
                })
        elif isinstance(msg, ToolMessage):
            steps.append(_result_step(msg))
    # 最后一个 thought 若出现在所有工具调用之后，它就是最终回复
    last_call = max((i for i, s in enumerate(steps) if s["type"] == "tool_call"), default=-1)
    for i in range(len(steps) - 1, -1, -1):
        if steps[i]["type"] == "thought":
            if i > last_call:
                steps[i]["type"] = "reply"
            break
    return steps


def describe_request(req: dict[str, Any], caller_id: str) -> list[str]:
    """给确认界面用的人类可读描述。"""
    name = req.get("name", "?")
    args = req.get("args") or {}
    labels = {
        "cancel_order": "取消订单",
        "submit_refund": "提交退款",
    }
    lines = [
        f"即将执行：{labels.get(name, name)}",
        f"参数：{json.dumps(args, ensure_ascii=False, default=str)}",
    ]
    order_id = args.get("order_id")
    if order_id:
        try:
            info = service.query_orders(caller_id, order_id=order_id)["orders"][0]
            lines.append(f"订单：{info['item_name']}  {info['amount']}  {info['status_label']}")
            if name == "submit_refund":
                lines.append(f"可退余额：{info['refundable_balance']}")
        except Exception:  # noqa: BLE001
            pass
    return lines


Approver = Callable[[list[dict[str, Any]], list[str]], Awaitable[Command]]


@dataclass
class TurnResult:
    reply: str
    steps: list[Step] = field(default_factory=list)
    messages: int = 0


class ConversationRunner:
    """持有一个 agent 实例与已读游标，按轮驱动。"""

    def __init__(
        self,
        agent,
        config: dict[str, Any],
        caller: dict[str, str],
        *,
        channel: str,
        session_id: str,
        verbose: bool = False,
        echo: Callable[[str], None] | None = None,
    ) -> None:
        self.agent = agent
        self.config = config
        self.caller = caller
        self.channel = channel
        self.session_id = session_id
        self.verbose = verbose
        self.echo = echo or (lambda _msg: None)
        self.seen = 0
        self.turns = 0

    # ---------------- 内部 ----------------
    async def _drive(self, payload, approver: Approver | None):
        final = None
        new_messages: list[BaseMessage] = []
        async for state in self.agent.astream(payload, self.config, stream_mode="values"):
            final = state
            fresh = state["messages"][self.seen:]
            for msg in fresh:
                self._echo_step(msg)
            new_messages.extend(fresh)
            self.seen = len(state["messages"])

        for _ in range(5):  # 防御：中断反复出现
            snapshot = await self.agent.aget_state(self.config)
            pending = getattr(snapshot, "interrupts", ()) or ()
            if not pending:
                break
            value = pending[0].value or {}
            requests = value.get("action_requests", []) or []
            lines: list[str] = []
            for req in requests:
                lines.extend(describe_request(req, self.caller["caller_id"]))
            if approver is None:
                # 先把已经发生的步骤落审计，再挂起。
                # 否则"发起调用 -> 等待确认"这一段在审计里是空白，
                # 只留下确认之后的结果，链条断掉。
                done = [s for s in collect_steps(new_messages)
                        if s["type"] in ("thought", "tool_call", "tool_result")]
                self._log_steps(done)
                raise PendingApproval(requests, lines)
            command = await approver(requests, lines)
            async for state in self.agent.astream(command, self.config, stream_mode="values"):
                final = state
                fresh = state["messages"][self.seen:]
                for msg in fresh:
                    self._echo_step(msg)
                new_messages.extend(fresh)
                self.seen = len(state["messages"])

        return final, new_messages

    def _echo_step(self, msg: BaseMessage) -> None:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for call in msg.tool_calls:
                args = json.dumps(call.get("args", {}), ensure_ascii=False, default=str)
                self.echo(f"   调用 {call['name']} {args}")
        elif isinstance(msg, ToolMessage):
            step = _result_step(msg)
            mark = "拒绝" if step["status"] == "ERROR" else "结果"
            self.echo(f"   {mark} {step['name']}: {step['text']}")
            if self.verbose:
                self.echo(f"   raw: {preview(step.get('raw', ''), 600)}")

    # ---------------- 对外 ----------------
    async def sync_seen(self) -> None:
        """从检查点对齐已读游标。

        网页端每次请求都会重建 agent，但会话状态存在共享的 checkpointer 里。
        不对齐的话，续跑时会把整段历史当成新消息重放一遍，步骤会翻倍。
        """
        state = await self.agent.aget_state(self.config)
        messages = (getattr(state, "values", None) or {}).get("messages", [])
        self.seen = len(messages)

    async def send(self, text: str, approver: Approver | None = None) -> TurnResult:
        self.turns += 1
        started = time.monotonic()
        audit.log(
            audit.KIND_USER_MESSAGE,
            {"text": text, "turn": self.turns},
            session_id=self.session_id,
            user_id=self.caller["caller_id"],
            user_name=self.caller["caller_name"],
            channel=self.channel,
        )
        # 先落用户消息：后面的驱动可能因等待确认而中断，
        # 若等到最后才写，用户会看到"自己的话消失了、只剩一条待确认操作"。
        audit.append_message(self.session_id, "user", text)

        try:
            final, fresh = await self._drive({"messages": [("user", text)]}, approver)
        except PendingApproval:
            raise
        except Exception as exc:  # noqa: BLE001
            audit.log(
                audit.KIND_ERROR,
                {"stage": "agent_turn", "error": f"{type(exc).__name__}: {exc}"},
                session_id=self.session_id,
                user_id=self.caller["caller_id"],
                user_name=self.caller["caller_name"],
                channel=self.channel,
                status="ERROR",
            )
            raise

        steps = collect_steps(fresh)
        reply = ""
        for step in reversed(steps):
            if step["type"] == "reply":
                reply = step["text"]
                break
        if not reply and final:
            reply = text_of(final["messages"][-1].content).strip()

        self._log_steps(steps, int((time.monotonic() - started) * 1000))
        audit.append_message(self.session_id, "assistant", reply, steps)
        return TurnResult(reply=reply, steps=steps, messages=self.seen)

    async def resume(self, approve: bool, note: str = "") -> TurnResult:
        """对挂起的写操作给出决定，继续跑完这一轮。"""
        snapshot = await self.agent.aget_state(self.config)
        pending = getattr(snapshot, "interrupts", ()) or ()
        if not pending:
            return TurnResult(reply="当前没有待确认的操作。")
        requests = (pending[0].value or {}).get("action_requests", []) or []

        if approve:
            decisions = [{"type": "approve"} for _ in requests]
            audit.log(
                audit.KIND_HUMAN_ACTION,
                {"action": "CONFIRM", "action_label": "用户确认执行",
                 "requests": requests, "note": note},
                session_id=self.session_id,
                user_id=self.caller["caller_id"],
                user_name=self.caller["caller_name"],
                channel=self.channel,
            )
        else:
            decisions = [
                {"type": "reject",
                 "message": "用户拒绝了该操作，请如实告知用户操作未执行，不要重复尝试。"}
                for _ in requests
            ]
            audit.log(
                audit.KIND_HUMAN_ACTION,
                {"action": "DECLINE", "action_label": "用户取消执行",
                 "requests": requests, "note": note},
                session_id=self.session_id,
                user_id=self.caller["caller_id"],
                user_name=self.caller["caller_name"],
                channel=self.channel,
            )

        final, fresh = await self._drive(Command(resume={"decisions": decisions}), None)
        steps = collect_steps(fresh)
        reply = ""
        for step in reversed(steps):
            if step["type"] == "reply":
                reply = step["text"]
                break
        if not reply and final:
            reply = text_of(final["messages"][-1].content).strip()

        self._log_steps(steps)
        audit.append_message(self.session_id, "assistant", reply, steps)
        return TurnResult(reply=reply, steps=steps, messages=self.seen)

    def _log_steps(self, steps: list[Step], elapsed_ms: int | None = None) -> None:
        for step in steps:
            if step["type"] == "thought":
                audit.log(audit.KIND_AGENT_THOUGHT, {"text": step["text"]},
                          session_id=self.session_id,
                          user_id=self.caller["caller_id"],
                          user_name=self.caller["caller_name"],
                          channel=self.channel)
            elif step["type"] == "tool_call":
                audit.log(audit.KIND_TOOL_CALL,
                          {"tool": step["name"], "args": step["args"]},
                          session_id=self.session_id,
                          user_id=self.caller["caller_id"],
                          user_name=self.caller["caller_name"],
                          channel=self.channel)
            elif step["type"] == "tool_result":
                audit.log(audit.KIND_TOOL_RESULT,
                          {"tool": step["name"], "text": step["text"],
                           "raw": step.get("raw"), "code": step.get("code")},
                          session_id=self.session_id,
                          user_id=self.caller["caller_id"],
                          user_name=self.caller["caller_name"],
                          channel=self.channel,
                          status=step["status"])
            elif step["type"] == "reply":
                audit.log(audit.KIND_AGENT_REPLY, {"text": step["text"]},
                          session_id=self.session_id,
                          user_id=self.caller["caller_id"],
                          user_name=self.caller["caller_name"],
                          channel=self.channel,
                          elapsed_ms=elapsed_ms)
