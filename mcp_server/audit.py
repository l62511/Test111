"""操作审计：一条只追加的事件流，记录"谁在什么时候做了什么、结果如何"。

为什么单独一个模块而不是随手 print 日志：

- Agent 的推理过程是**有状态、多步**的。用户说一句话，背后可能是
  「查订单 → 判定规则 → 提交申请」三次工具调用，中间任何一步失败都会
  影响最终话术。排查问题时需要按顺序还原这串步骤，而不是翻散落的日志行。
- 人工审批是**不可逆动作**。谁批的、批之前 Agent 判断依据是什么、
  备注写了什么，都必须能追溯，否则出了资损说不清责任。

事件按 seq 单调递增，只追加不改写。kind 取值见 KINDS。

并发说明：Agent 的 MCP 子进程和 Web 后台会同时写这个文件，
写入统一走 store.transaction()，靠文件锁串行化。
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any, Iterable

from . import store

# 事件类型
KIND_USER_MESSAGE = "USER_MESSAGE"     # 用户指令
KIND_AGENT_THOUGHT = "AGENT_THOUGHT"   # 模型文本输出（含推理说明）
KIND_TOOL_CALL = "TOOL_CALL"           # 工具调用请求（名称 + 参数）
KIND_TOOL_RESULT = "TOOL_RESULT"       # 工具返回（摘要 + 成功/失败）
KIND_AGENT_REPLY = "AGENT_REPLY"       # 最终回复
KIND_RULE_DECISION = "RULE_DECISION"   # 规则引擎自动判定
KIND_HUMAN_ACTION = "HUMAN_ACTION"     # 人工审批/驳回/解锁
KIND_ERROR = "ERROR"                   # 异常

KINDS = (
    KIND_USER_MESSAGE, KIND_AGENT_THOUGHT, KIND_TOOL_CALL, KIND_TOOL_RESULT,
    KIND_AGENT_REPLY, KIND_RULE_DECISION, KIND_HUMAN_ACTION, KIND_ERROR,
)

KIND_LABEL = {
    KIND_USER_MESSAGE: "用户指令",
    KIND_AGENT_THOUGHT: "Agent 思考",
    KIND_TOOL_CALL: "工具调用",
    KIND_TOOL_RESULT: "工具结果",
    KIND_AGENT_REPLY: "Agent 回复",
    KIND_RULE_DECISION: "规则判定",
    KIND_HUMAN_ACTION: "人工操作",
    KIND_ERROR: "异常",
}

# 通道
CHANNEL_CLI = "CLI"
CHANNEL_WEB = "WEB"
CHANNEL_SYSTEM = "SYSTEM"

MAX_PAYLOAD_CHARS = 4000


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _trim(value: Any) -> Any:
    """payload 里超长的字符串截断，避免一次工具返回把审计文件撑爆。"""
    if isinstance(value, str):
        return value if len(value) <= MAX_PAYLOAD_CHARS else value[:MAX_PAYLOAD_CHARS] + "…(截断)"
    if isinstance(value, dict):
        return {k: _trim(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_trim(v) for v in value[:50]]
    return value


def log(
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    user_name: str | None = None,
    channel: str = CHANNEL_SYSTEM,
    status: str = "OK",
    elapsed_ms: int | None = None,
) -> dict[str, Any]:
    """追加一条事件，返回落盘后的记录。"""
    if kind not in KINDS:
        raise ValueError(f"未知事件类型 {kind}")
    ts = _now()
    with store.transaction(store.AUDIT_FILE, store.empty_audit()) as data:
        data["seq"] += 1
        seq = data["seq"]
        event = {
            "event_id": f"EV{seq:06d}{secrets.token_hex(2).upper()}",
            "seq": seq,
            "ts": ts,
            "kind": kind,
            "kind_label": KIND_LABEL[kind],
            "status": status,
            "channel": channel,
            "session_id": session_id,
            "user_id": user_id,
            "user_name": user_name,
            "elapsed_ms": elapsed_ms,
            "payload": _trim(payload or {}),
        }
        data["events"][event["event_id"]] = event
    return event


def log_many(events: Iterable[tuple[str, dict[str, Any], str]]) -> None:
    """批量追加，减少锁竞争。events 元素为 (kind, payload, status)。"""
    rows = [e for e in events if e[0] in KINDS]
    if not rows:
        return
    ts = _now()
    with store.transaction(store.AUDIT_FILE, store.empty_audit()) as data:
        for kind, payload, status in rows:
            data["seq"] += 1
            seq = data["seq"]
            event_id = f"EV{seq:06d}{secrets.token_hex(2).upper()}"
            data["events"][event_id] = {
                "event_id": event_id,
                "seq": seq,
                "ts": ts,
                "kind": kind,
                "kind_label": KIND_LABEL[kind],
                "status": status,
                "channel": CHANNEL_SYSTEM,
                "session_id": None,
                "user_id": None,
                "user_name": None,
                "elapsed_ms": None,
                "payload": _trim(payload),
            }


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------
def list_events(
    session_id: str | None = None,
    kind: str | None = None,
    user_id: str | None = None,
    status: str | None = None,
    limit: int = 200,
    newest_first: bool = True,
) -> list[dict[str, Any]]:
    data = store.read_only(store.AUDIT_FILE, store.empty_audit())
    rows = list(data["events"].values())
    if session_id:
        rows = [e for e in rows if e.get("session_id") == session_id]
    if kind:
        rows = [e for e in rows if e["kind"] == kind]
    if user_id:
        rows = [e for e in rows if e.get("user_id") == user_id]
    if status:
        rows = [e for e in rows if e.get("status") == status]
    rows.sort(key=lambda e: e["seq"], reverse=newest_first)
    limit = max(1, min(int(limit or 200), 1000))
    return rows[:limit]


def timeline(session_id: str) -> list[dict[str, Any]]:
    """单会话的完整时间线，按发生顺序。"""
    return list_events(session_id=session_id, limit=1000, newest_first=False)


def stats() -> dict[str, Any]:
    data = store.read_only(store.AUDIT_FILE, store.empty_audit())
    rows = list(data["events"].values())
    today = datetime.now().strftime("%Y-%m-%d")
    return {
        "total": len(rows),
        "today": len([e for e in rows if (e.get("ts") or "").startswith(today)]),
        "errors": len([e for e in rows if e.get("status") == "ERROR"]),
        "tool_calls": len([e for e in rows if e["kind"] == KIND_TOOL_CALL]),
        "human_actions": len([e for e in rows if e["kind"] == KIND_HUMAN_ACTION]),
        "sessions": len({e["session_id"] for e in rows if e.get("session_id")}),
    }


# --------------------------------------------------------------------------
# 会话
# --------------------------------------------------------------------------
def get_session(session_id: str) -> dict[str, Any] | None:
    data = store.read_only(store.SESSIONS_FILE, store.empty_sessions())
    return data["sessions"].get(session_id)


def ensure_session(
    session_id: str, user_id: str, user_name: str, channel: str
) -> dict[str, Any]:
    """取会话，不存在则新建。返回副本，调用方随意修改。"""
    with store.transaction(store.SESSIONS_FILE, store.empty_sessions()) as data:
        session = data["sessions"].get(session_id)
        if session is None:
            data["seq"] += 1
            session = {
                "session_id": session_id,
                "user_id": user_id,
                "user_name": user_name,
                "channel": channel,
                "seq": data["seq"],
                "created_at": _now(),
                "updated_at": _now(),
                "title": None,
                "messages": [],
            }
            data["sessions"][session_id] = session
        return dict(session)


def append_message(
    session_id: str, role: str, content: str, steps: list[dict[str, Any]] | None = None
) -> None:
    """向会话追加一条消息。steps 是这条回复对应的 ReAct 步骤。"""
    with store.transaction(store.SESSIONS_FILE, store.empty_sessions()) as data:
        session = data["sessions"].get(session_id)
        if session is None:
            return
        session["messages"].append({
            "role": role,
            "content": content,
            "steps": steps or [],
            "ts": _now(),
        })
        session["updated_at"] = _now()
        if role == "user" and not session.get("title"):
            session["title"] = content[:24]


def list_sessions(limit: int = 50) -> list[dict[str, Any]]:
    data = store.read_only(store.SESSIONS_FILE, store.empty_sessions())
    rows = []
    for s in data["sessions"].values():
        turns = len([m for m in s["messages"] if m["role"] == "user"])
        last = s["messages"][-1]["content"] if s["messages"] else ""
        rows.append({
            "session_id": s["session_id"],
            "user_id": s["user_id"],
            "user_name": s["user_name"],
            "channel": s["channel"],
            "title": s.get("title") or "（空会话）",
            "turns": turns,
            "updated_at": s["updated_at"],
            "last_line": " ".join(str(last).split())[:60],
        })
    rows.sort(key=lambda r: r["updated_at"], reverse=True)
    return rows[:limit]
