"""商家审批后台。

启动:
    python -m web.app
    python -m web.app --port 8080

认证状态存在签名 cookie 里：值形如 `<user_id>.<hmac>`，
签名密钥来自 MERCHANT_SECRET 环境变量，未配置时用一个固定演示密钥。
密钥变更会让所有已登录会话失效——这是期望行为。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
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

from mcp_server import service  # noqa: E402

WEB_DIR = Path(__file__).resolve().parent
SECRET = os.getenv("MERCHANT_SECRET", "dev-only-change-me-in-production")
COOKIE_NAME = "merchant_session"

app = FastAPI(title="订单退款审批后台", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
templates = Jinja2Templates(directory=WEB_DIR / "templates")


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


def require_merchant(request: Request) -> dict[str, Any]:
    """依赖项：未登录或角色不符时跳到登录页。"""
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


class _Redirect(Exception):
    def __init__(self, url: str) -> None:
        self.url = url


def _redirect(url: str) -> _Redirect:
    return _Redirect(url)


@app.exception_handler(_Redirect)
async def _handle_redirect(request: Request, exc: _Redirect):
    return RedirectResponse(exc.url, status_code=303)


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = "", next: str = ""):
    return templates.TemplateResponse(
        request, "login.html", {"error": error, "next": next}
    )


@app.post("/login")
async def do_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    try:
        user = service.authenticate(username, password, role="merchant")
    except service.BizError as exc:
        return RedirectResponse(
            f"/login?error={quote(exc.message)}", status_code=303
        )
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


@app.get("/refunds", response_class=HTMLResponse)
async def refunds_page(
    request: Request, order_id: str = "", status: str = "", msg: str = "", level: str = "ok"
):
    user = require_merchant(request)
    items = service.list_refunds(
        order_id=order_id or None, status=status or None, limit=100
    )
    stats = service.dashboard_stats()
    return templates.TemplateResponse(
        request, "refunds.html",
        {
            "user": user, "items": items, "stats": stats,
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
    stats = service.dashboard_stats()
    return templates.TemplateResponse(
        request, "orders.html",
        {
            "user": user, "orders": orders, "stats": stats, "page": "orders",
            "msg": msg, "level": level,
        },
    )


# --------------------------------------------------------------------------
# 操作
# --------------------------------------------------------------------------
@app.post("/refunds/{refund_id}/approve")
async def approve(
    request: Request, refund_id: str, note: str = Form("")
):
    user = require_merchant(request)
    try:
        res = service.approve_refund(refund_id, user["user_id"], note=note)
        target = f"/queue?msg={quote(res['message'])}&level=ok"
    except service.BizError as exc:
        target = f"/queue?msg={quote(exc.message)}&level=err"
    return RedirectResponse(target, status_code=303)


@app.post("/refunds/{refund_id}/reject")
async def reject(
    request: Request, refund_id: str, note: str = Form("")
):
    user = require_merchant(request)
    try:
        if not note.strip():
            raise service.BizError("NOTE_REQUIRED", "驳回必须填写原因", "")
        res = service.reject_refund(refund_id, user["user_id"], note=note)
        target = f"/queue?msg={quote(res['message'])}&level=warn"
    except service.BizError as exc:
        target = f"/queue?msg={quote(exc.message)}&level=err"
    return RedirectResponse(target, status_code=303)


@app.post("/orders/{order_id}/unlock")
async def unlock(request: Request, order_id: str, note: str = Form("")):
    user = require_merchant(request)
    try:
        res = service.unlock_order(order_id, user["user_id"], note=note)
        level = "ok" if res["changed"] else "warn"
        target = f"/orders?msg={quote(res['message'])}&level={level}"
    except service.BizError as exc:
        target = f"/orders?msg={quote(exc.message)}&level=err"
    return RedirectResponse(target, status_code=303)


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="商家退款审批后台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    print(f"  退款审批后台  http://{args.host}:{args.port}")
    print("  商家账号：merchant / admin123")
    uvicorn.run(
        "web.app:app" if args.reload else app,
        host=args.host, port=args.port, reload=args.reload, log_level="warning",
    )


if __name__ == "__main__":
    main()
