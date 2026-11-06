"""商家后台：退款审批、Agent 会话、售后分析、审计追溯。

启动:
    python -m web.app
    python -m web.app --port 8080 --reload

认证状态存在签名 cookie 里：值形如 `<user_id>.<hmac>`，
签名密钥来自 MERCHANT_SECRET 环境变量，未配置时用一个固定演示密钥。
密钥变更会让所有已登录会话失效——这是期望行为。

Agent 会话页说明：
商家可以选一个顾客身份发起对话，用于核验 Agent 的行为是否符合规则。
会话状态放在进程内的 checkpointer 里，进程重启后历史消息仍在（存 sessions.json），
但"当前挂起待确认的写操作"会丢失——这是有意取舍，避免把半截事务写进磁盘。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

load_dotenv(BASE_DIR / ".env")

from fastapi import FastAPI, Form, Request  # noqa: E402
from fastapi.responses import HTMLResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

from mcp_server import analytics, audit, rules, service  # noqa: E402

WEB_DIR = Path(__file__).resolve().parent
SECRET = os.getenv("MERCHANT_SECRET", "dev-only-change-me-in-production")
COOKIE_NAME = "merchant_session"

app = FastAPI(title="订单客服 Agent 后台", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
templates = Jinja2Templates(directory=WEB_DIR / "templates")

# 网页会话的挂起确认状态：session_id -> 待确认动作列表
_PENDING: dict[str, list[dict[str, Any]]] = {}
_CHECKPOINTER = None


def _checkpointer():
    """懒加载，避免没配 LLM 时连页面都打不开。"""
    global _CHECKPOINTER
    if _CHECKPOINTER is None:
        from langgraph.checkpoint.memory import InMemorySaver

        _CHECKPOINTER = InMemorySaver()
    return _CHECKPOINTER


# --------------------------------------------------------------------------
# 会话
# --------------------------------------------------------------------------
def _sign(value: str) -> str:
    sig = hmac.new(SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()[:40]
    return f"{value}.{sig}"


def _verify(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    value, sig = token.rsplit(".", 1)
    expected = hmac.new(SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()[:40]
    if not hmac.compare_digest(sig, expected):
        return None
    return value


class _Redirect(Exception):
    def __init__(self, url: str) -> None:
        self.url = url


def _redirect(url: str) -> _Redirect:
    return _Redirect(url)


def require_merchant(request: Request) -> dict[str, Any]:
    user_id = _verify(request.cookies.get(COOKIE_NAME))
    if not user_id:
        raise _redirect("/login")
    try:
        user = service.get_user(user_id)
    except service.BizError:
        raise _redirect("/login") from None
    if user["role"] != "merchant":
        raise _redirect("/login")
    return user


@app.exception_handler(_Redirect)
async def _handle_redirect(request: Request, exc: _Redirect):
    return RedirectResponse(exc.url, status_code=303)


def _flash(path: str, msg: str, level: str = "ok") -> RedirectResponse:
    sep = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{sep}msg={quote(msg)}&level={level}", status_code=303)


# --------------------------------------------------------------------------
# 登录
# --------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
async def do_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    try:
        user = service.authenticate(username, password, role="merchant")
    except service.BizError as exc:
        return RedirectResponse(f"/login?error={quote(exc.message)}", status_code=303)
    response = RedirectResponse("/queue", status_code=303)
    response.set_cookie(
        COOKIE_NAME, _sign(user["user_id"]),
        httponly=True, samesite="lax", max_age=8 * 3600,
    )
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    raise _redirect("/queue")


# --------------------------------------------------------------------------
# 人工审核中心
# --------------------------------------------------------------------------
@app.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request, msg: str = "", level: str = "ok"):
    user = require_merchant(request)
    items = service.pending_queue()
    stats = service.dashboard_stats()
    return templates.TemplateResponse(
        request, "queue.html",
        {
            "user": user, "items": items, "stats": stats,
            "msg": msg, "level": level, "page": "queue",
        },
    )


@app.get("/review/{refund_id}", response_class=HTMLResponse)
async def review_page(request: Request, refund_id: str, msg: str = "", level: str = "ok"):
    user = require_merchant(request)
    try:
        detail = service.review_detail(refund_id)
    except service.BizError as exc:
        return _flash("/queue", exc.message, "err")
    return templates.TemplateResponse(
        request, "review.html",
        {
            "user": user, "d": detail, "stats": service.dashboard_stats(),
            "msg": msg, "level": level, "page": "queue",
        },
    )


@app.post("/refunds/{refund_id}/approve")
async def approve(request: Request, refund_id: str, note: str = Form(""),
                  back: str = Form("/queue")):
    user = require_merchant(request)
    try:
        res = service.approve_refund(refund_id, user["user_id"], note=note)
        return _flash(back, res["message"], "ok")
    except service.BizError as exc:
        return _flash(back, exc.message, "err")


@app.post("/refunds/{refund_id}/reject")
async def reject(request: Request, refund_id: str, note: str = Form(""),
                 back: str = Form("/queue")):
    user = require_merchant(request)
    try:
        if not note.strip():
            raise service.BizError("NOTE_REQUIRED", "驳回必须填写原因", "")
        res = service.reject_refund(refund_id, user["user_id"], note=note)
        return _flash(back, res["message"], "warn")
    except service.BizError as exc:
        return _flash(back, exc.message, "err")


@app.get("/refunds", response_class=HTMLResponse)
async def refunds_page(
    request: Request, order_id: str = "", status: str = "",
    msg: str = "", level: str = "ok",
):
    user = require_merchant(request)
    items = service.list_refunds(
        order_id=order_id or None, status=status or None, limit=100
    )
    return templates.TemplateResponse(
        request, "refunds.html",
        {
            "user": user, "items": items, "stats": service.dashboard_stats(),
            "msg": msg, "level": level, "page": "refunds",
            "filter_order": order_id, "filter_status": status,
        },
    )


@app.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request, msg: str = "", level: str = "ok"):
    user = require_merchant(request)
    orders = []
    for u in service.list_users(role="customer"):
        res = service.query_orders(u["user_id"], limit=20)
        for o in res["orders"]:
            o["_owner"] = u["name"]
            orders.append(o)
    orders.sort(key=lambda o: o["created_at"], reverse=True)
    return templates.TemplateResponse(
        request, "orders.html",
        {
            "user": user, "orders": orders, "stats": service.dashboard_stats(),
            "msg": msg, "level": level, "page": "orders",
        },
    )


@app.post("/orders/{order_id}/unlock")
async def unlock(request: Request, order_id: str, note: str = Form("")):
    user = require_merchant(request)
    try:
        res = service.unlock_order(order_id, user["user_id"], note=note)
        return _flash("/orders", res["message"], "ok" if res["changed"] else "warn")
    except service.BizError as exc:
        return _flash("/orders", exc.message, "err")


# --------------------------------------------------------------------------
# 售后数据分析
# --------------------------------------------------------------------------
@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request, period: str = "ALL", msg: str = "", level: str = "ok"):
    user = require_merchant(request)
    data = analytics.summary(period)
    peak = max([p["amount_cents"] for p in data["trend"]] or [1]) or 1
    return templates.TemplateResponse(
        request, "analytics.html",
        {
            "user": user, "d": data, "stats": service.dashboard_stats(),
            "msg": msg, "level": level, "page": "analytics",
            "peak": peak, "period": data["period"]["key"],
            "issue_label": rules.ISSUE_LABEL,
        },
    )


# --------------------------------------------------------------------------
# 审计
# --------------------------------------------------------------------------
@app.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request, session_id: str = "", kind: str = "",
    status: str = "", msg: str = "", level: str = "ok",
):
    user = require_merchant(request)
    events = audit.list_events(
        session_id=session_id or None, kind=kind or None,
        status=status or None, limit=300,
    )
    return templates.TemplateResponse(
        request, "audit.html",
        {
            "user": user, "events": events, "stats": service.dashboard_stats(),
            "audit_stats": audit.stats(), "sessions": audit.list_sessions(20),
            "kind_label": audit.KIND_LABEL,
            "msg": msg, "level": level, "page": "audit",
            "filter_session": session_id, "filter_kind": kind, "filter_status": status,
        },
    )


# --------------------------------------------------------------------------
# Agent 会话
# --------------------------------------------------------------------------
@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request, sid: str = "", as_user: str = "",
                    new: str = "", msg: str = "", level: str = "ok"):
    user = require_merchant(request)
    customers = service.list_users(role="customer")
    session = audit.get_session(sid) if sid else None
    if session is None and not new:
        # 没指定会话时默认打开最近一次，方便接着看
        sessions = audit.list_sessions(20)
        if sessions:
            session = audit.get_session(sessions[0]["session_id"])
    target = as_user or (session["user_id"] if session else
                         (customers[0]["user_id"] if customers else ""))
    return templates.TemplateResponse(
        request, "chat.html",
        {
            "user": user, "customers": customers, "session": session,
            "sessions": audit.list_sessions(20),
            "target": target,
            "pending": _PENDING.get(session["session_id"]) if session else None,
            "stats": service.dashboard_stats(), "msg": msg, "level": level,
            "page": "chat",
        },
    )


async def _build_runner(target_user: str, session_id: str):
    """按顾客身份装配 agent 与运行器。"""
    from langchain.agents import create_agent
    from langchain.agents.middleware import HumanInTheLoopMiddleware

    from agent.llm import build_llm
    from agent.prompt import build_system_prompt
    from agent.runner import INTERRUPT_ON, ConversationRunner
    from agent.tools_bridge import mcp_tools

    caller = service.resolve_caller(target_user)
    ctx = mcp_tools(target_user)
    tools = await ctx.__aenter__()
    agent = create_agent(
        model=build_llm(),
        tools=tools,
        system_prompt=build_system_prompt(caller["caller_name"]),
        middleware=[HumanInTheLoopMiddleware(
            interrupt_on=INTERRUPT_ON,
            description_prefix="不可逆的订单写操作",
        )],
        checkpointer=_checkpointer(),
    )
    runner = ConversationRunner(
        agent, {"configurable": {"thread_id": session_id}}, caller,
        channel=audit.CHANNEL_WEB, session_id=session_id,
    )
    return runner, ctx


@app.post("/chat/send")
async def chat_send(
    request: Request,
    text: str = Form(...),
    sid: str = Form(""),
    as_user: str = Form(""),
):
    require_merchant(request)
    text = text.strip()
    if not text:
        return _flash(f"/chat?sid={sid}", "消息不能为空", "warn")

    from agent.runner import PendingApproval

    session_id = sid or f"web-{as_user or 'u_1001'}-{datetime.now():%Y%m%d%H%M%S}"
    target = as_user or session_id.split("-")[1]
    try:
        target_name = service.get_user(target)["name"]
    except service.BizError:
        return _flash("/chat", f"未知顾客身份 {target}", "err")
    audit.ensure_session(session_id, target, target_name, audit.CHANNEL_WEB)

    try:
        runner, ctx = await _build_runner(target, session_id)
    except Exception as exc:  # noqa: BLE001
        return _flash(f"/chat?sid={session_id}", f"Agent 启动失败：{exc}", "err")

    try:
        if sid:
            await runner.sync_seen()
        await runner.send(text, approver=None)
        _PENDING.pop(session_id, None)
    except PendingApproval as pending:
        _PENDING[session_id] = pending.requests
    except Exception as exc:  # noqa: BLE001
        # 注意：退出上下文时不能把异常再抛回去，否则 __aexit__ 会把它重新抛出，
        # 本该显示友好提示的地方会变成 500。
        await ctx.__aexit__(None, None, None)
        return _flash(f"/chat?sid={session_id}", f"本轮出错：{exc}", "err")
    await ctx.__aexit__(None, None, None)
    return RedirectResponse(f"/chat?sid={session_id}", status_code=303)


@app.post("/chat/confirm")
async def chat_confirm(
    request: Request,
    sid: str = Form(...),
    as_user: str = Form(...),
    decision: str = Form(...),
):
    require_merchant(request)
    approve = decision == "approve"
    try:
        runner, ctx = await _build_runner(as_user, sid)
    except Exception as exc:  # noqa: BLE001
        return _flash(f"/chat?sid={sid}", f"Agent 启动失败：{exc}", "err")

    try:
        await runner.sync_seen()
        await runner.resume(approve)
        _PENDING.pop(sid, None)
    except Exception as exc:  # noqa: BLE001
        await ctx.__aexit__(None, None, None)
        return _flash(f"/chat?sid={sid}", f"执行失败：{exc}", "err")

    await ctx.__aexit__(None, None, None)
    return RedirectResponse(f"/chat?sid={sid}", status_code=303)


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="订单客服 Agent 后台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    print(f"  后台地址  http://{args.host}:{args.port}")
    print("  商家账号：merchant / admin123")
    print("  六个页面  待审批 / 审核详情 / Agent 会话 / 售后分析 / 审计")
    uvicorn.run(
        "web.app:app" if args.reload else app,
        host=args.host, port=args.port, reload=args.reload, log_level="warning",
    )


if __name__ == "__main__":
    main()
