"""行级访问与业务规则（JSON 文件后端）。

设计要点：

1. **身份不从参数来**。调用方身份由进程环境注入或登录态确定，
   所有查询都按 owner_id 过滤，越权订单统一表现为「不存在」。
2. **写操作走一个持锁事务**。读-改-写在同一个文件锁内完成，
   不存在「先查再改」的窗口期。
3. **退款由规则引擎判定**。模型不传金额、也不决定走自动还是人工：
   调用方只提供退款诉求，rules.evaluate() 算出金额、风险分与目标通道，
   未发货的低风险单直接放款，其余进商家队列。
4. **退款金额累加**。订单上记 refunded_cents，每次退款前校验
   「已退 + 本次 ≤ 实付」，支持多次部分退款，但累计绝不超额。
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

from . import analytics, audit, rules, store

# 订单状态
VALID_STATUS = ("CREATED", "PAID", "CANCELLED", "COMPLETED", "REFUNDED")
CANCELLABLE_STATUS = ("CREATED", "PAID")
# 可以发起退款的状态：已取消（售前）或已完成（签收后退货）
REFUNDABLE_STATUS = ("CANCELLED", "COMPLETED")

STATUS_LABEL = {
    "CREATED": "待支付",
    "PAID": "已支付",
    "CANCELLED": "已取消",
    "COMPLETED": "已完成",
    "REFUNDED": "已退款",
}

REFUND_STATUS_LABEL = {
    "PENDING": "待审批",
    "APPROVED": "已通过",
    "REJECTED": "已驳回",
}

# 错误码，Agent 与后台据此生成面向用户的话术
E_NOT_FOUND = "ORDER_NOT_FOUND"
E_ALREADY_CANCELLED = "ALREADY_CANCELLED"
E_ALREADY_REFUNDED = "ALREADY_REFUNDED"
E_NOT_REFUNDABLE = "ORDER_NOT_REFUNDABLE"
E_STATE = "INVALID_STATE"
E_AMOUNT = "AMOUNT_EXCEEDED"
E_INVALID = "INVALID_ARGUMENT"
E_UNKNOWN_USER = "UNKNOWN_CALLER"
E_AUTH_FAILED = "AUTH_FAILED"
E_PENDING_EXISTS = "PENDING_REFUND_EXISTS"
E_REFUND_LOCKED = "REFUND_LOCKED_AFTER_REJECT"
E_REFUND_NOT_FOUND = "REFUND_NOT_FOUND"
E_REFUND_HANDLED = "REFUND_ALREADY_HANDLED"
E_FORBIDDEN = "FORBIDDEN"


class BizError(Exception):
    def __init__(self, code: str, message: str, hint: str = "",
                 extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.extra = extra or {}


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def yuan(cents: int | None) -> str:
    return "—" if cents is None else f"¥{cents / 100:,.2f}"


def _new_refund_id() -> str:
    return f"RF{datetime.now():%Y%m%d%H%M%S}{secrets.token_hex(2).upper()}"


def _to_cents(amount: float | int | str | None) -> int | None:
    if amount is None or amount == "":
        return None
    try:
        return int(round(float(amount) * 100))
    except (TypeError, ValueError):
        raise BizError(E_INVALID, f"金额格式不正确: {amount}") from None


# --------------------------------------------------------------------------
# 账号
# --------------------------------------------------------------------------
def authenticate(username: str, password: str, role: str | None = None) -> dict[str, Any]:
    """账号密码登录。用户名不区分大小写，密码错误与用户不存在返回同一错误。"""
    username = (username or "").strip().lower()
    if not username or not password:
        raise BizError(E_AUTH_FAILED, "请输入账号和密码", "登录信息不能为空")

    data = store.read_only(store.USERS_FILE, store.empty_users())
    user = next(
        (u for u in data["users"].values() if u["username"].lower() == username), None
    )
    # 用户不存在时也走一次哈希校验，避免通过响应时间枚举账号
    stored = user["password_hash"] if user else store.hash_password("__nobody__")
    if user is None or not store.verify_password(password, stored):
        raise BizError(E_AUTH_FAILED, "账号或密码不正确", "请确认后重试")
    if role and user["role"] != role:
        raise BizError(
            E_FORBIDDEN,
            "该账号无权访问商家后台" if user["role"] != "merchant" else "请使用顾客账号登录",
            "商家后台仅限商家账号",
        )
    return public_user(user)


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    """对外返回的用户信息，绝不含 password_hash。"""
    return {
        "user_id": user["user_id"],
        "username": user["username"],
        "name": user["name"],
        "role": user["role"],
    }


def get_user(user_id: str) -> dict[str, Any]:
    data = store.read_only(store.USERS_FILE, store.empty_users())
    user = data["users"].get(user_id)
    if user is None:
        raise BizError(E_UNKNOWN_USER, f"未知用户 {user_id}", "请检查账号配置")
    return public_user(user)


def resolve_caller(caller_id: str) -> dict[str, str]:
    """校验身份存在。调用方身份由进程环境注入，不接受模型传参。"""
    user = get_user(caller_id)
    return {"caller_id": user["user_id"], "caller_name": user["name"]}


def list_users(role: str | None = None) -> list[dict[str, Any]]:
    data = store.read_only(store.USERS_FILE, store.empty_users())
    users = [public_user(u) for u in data["users"].values()]
    if role:
        users = [u for u in users if u["role"] == role]
    return sorted(users, key=lambda u: u["user_id"])


# --------------------------------------------------------------------------
# 订单视图
# --------------------------------------------------------------------------
def _pending_refund(order_id: str, refunds: dict[str, Any]) -> dict[str, Any] | None:
    for r in refunds["refunds"].values():
        if r["order_id"] == order_id and r["status"] == "PENDING":
            return r
    return None


def _lock_of(order_id: str, refunds: dict[str, Any]) -> dict[str, Any] | None:
    lock = refunds.get("locks", {}).get(order_id)
    return lock if lock and lock.get("active") else None


def _shape(order: dict[str, Any], refunds: dict[str, Any]) -> dict[str, Any]:
    amount = order["amount_cents"]
    refunded = order.get("refunded_cents", 0)
    balance = amount - refunded
    shipped = order.get("shipped_at") is not None
    delivered = order.get("delivered_at") is not None
    pending = _pending_refund(order["order_id"], refunds)
    locked = _lock_of(order["order_id"], refunds) is not None

    can_refund = (
        order["status"] in REFUNDABLE_STATUS
        and balance > 0
        and pending is None
        and not locked
    )

    return {
        "order_id": order["order_id"],
        "item_name": order["item_name"],
        "item_desc": order.get("item_desc"),
        "amount": yuan(amount),
        "amount_cents": amount,
        "status": order["status"],
        "status_label": STATUS_LABEL[order["status"]],
        "created_at": order["created_at"],
        "paid_at": order.get("paid_at"),
        "shipped": shipped,
        "shipped_at": order.get("shipped_at"),
        "delivered": delivered,
        "delivered_at": order.get("delivered_at"),
        "shipping_state": (
            "DELIVERED" if delivered else ("SHIPPED" if shipped else "NOT_SHIPPED")
        ),
        "tracking_no": order.get("tracking_no"),
        "cancelled_at": order.get("cancelled_at"),
        "refunded_cents": refunded,
        "refunded_amount": yuan(refunded),
        "refundable_balance": yuan(balance),
        "refundable_balance_cents": balance,
        "refunded_at": order.get("refunded_at"),
        "can_cancel": order["status"] in CANCELLABLE_STATUS,
        "can_refund": can_refund,
        "refund_requires_approval": bool(shipped or delivered),
        "refund_locked": locked,
        "pending_refund": (
            {
                "refund_id": pending["refund_id"],
                "amount": yuan(pending["amount_cents"]),
                "status": REFUND_STATUS_LABEL[pending["status"]],
                "created_at": pending["created_at"],
            }
            if pending
            else None
        ),
    }


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------
def query_orders(
    caller_id: str,
    order_id: str | None = None,
    status: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    if order_id:
        order_id = order_id.strip().upper()
    if status and status not in VALID_STATUS:
        raise BizError(E_INVALID, f"不支持的状态值 {status}",
                       f"可选值: {', '.join(VALID_STATUS)}")
    limit = max(1, min(int(limit or 10), 20))

    orders = store.read_only(store.ORDERS_FILE, store.empty_orders())["orders"]
    refunds = store.read_only(store.REFUNDS_FILE, store.empty_refunds())

    matched = [
        o for o in orders.values()
        if o["owner_id"] == caller_id
        and (not order_id or o["order_id"] == order_id)
        and (not status or o["status"] == status)
    ]
    matched.sort(key=lambda o: o["created_at"], reverse=True)

    if order_id and not matched:
        raise BizError(
            E_NOT_FOUND,
            f"未找到订单 {order_id}",
            "该订单不存在，或不属于当前登录用户。不要向用户确认它是否属于他人。",
        )

    shaped = [_shape(o, refunds) for o in matched[:limit]]
    return {
        "ok": True,
        "count": len(shaped),
        "orders": shaped,
        "summary": _summary_line(shaped),
    }


def _summary_line(orders: list[dict[str, Any]]) -> str:
    if not orders:
        return "没有符合条件的订单"
    if len(orders) == 1:
        o = orders[0]
        return f"{o['order_id']} {o['item_name']} {o['amount']} {o['status_label']}"
    return f"共 {len(orders)} 笔订单"


# --------------------------------------------------------------------------
# 售后查询：退款记录 / 售后申请 / 历史售后情况
# --------------------------------------------------------------------------
def query_after_sales(caller_id: str, order_id: str | None = None) -> dict[str, Any]:
    """查当前用户的售后全貌：申请记录、审批结果、历史频次与风险画像。"""
    order_id = (order_id or "").strip().upper() or None

    orders = store.read_only(store.ORDERS_FILE, store.empty_orders())["orders"]
    refunds = store.read_only(store.REFUNDS_FILE, store.empty_refunds())
    locks = refunds.get("locks", {})

    mine = [o for o in orders.values() if o["owner_id"] == caller_id]
    my_orders = {o["order_id"] for o in mine}

    records = [
        r for r in refunds["refunds"].values()
        if r["order_id"] in my_orders and (order_id is None or r["order_id"] == order_id)
    ]
    records.sort(key=lambda r: r["seq"], reverse=True)

    if order_id and order_id not in my_orders:
        raise BizError(
            E_NOT_FOUND,
            f"未找到订单 {order_id}",
            "该订单不存在，或不属于当前登录用户。不要向用户确认它是否属于他人。",
        )

    approved = [r for r in records if r["status"] == "APPROVED"]
    pending = [r for r in records if r["status"] == "PENDING"]
    rejected = [r for r in records if r["status"] == "REJECTED"]

    items = []
    for r in records[:20]:
        order = orders.get(r["order_id"], {})
        lock = locks.get(r["order_id"])
        items.append({
            "refund_id": r["refund_id"],
            "order_id": r["order_id"],
            "item_name": order.get("item_name", "—"),
            "amount": yuan(r["amount_cents"]),
            "requested_amount": yuan(r.get("requested_cents") or r["amount_cents"]),
            "status": r["status"],
            "status_label": REFUND_STATUS_LABEL[r["status"]],
            "mode": r.get("mode"),
            "issue_type": r.get("issue_type", "OTHER"),
            "issue_label": rules.ISSUE_LABEL.get(r.get("issue_type", "OTHER"), "其他原因"),
            "reason": r["reason"],
            "created_at": r["created_at"],
            "reviewed_at": r.get("reviewed_at"),
            "reviewer_name": r.get("reviewer_name"),
            "review_note": r.get("review_note"),
            "decision": (r.get("decision") or {}).get("decision"),
            "decision_label": (r.get("decision") or {}).get("decision_label"),
            "risk_label": (r.get("decision") or {}).get("risk_label"),
            "locked": bool(lock and lock.get("active")),
        })

    total_refunded = sum(r["amount_cents"] for r in approved)
    summary = (
        f"共 {len(records)} 条售后记录：已通过 {len(approved)} 笔（合计 {yuan(total_refunded)}）、"
        f"待审批 {len(pending)} 笔、已驳回 {len(rejected)} 笔"
        if records
        else "该用户没有售后记录"
    )

    return {
        "ok": True,
        "count": len(items),
        "records": items,
        "summary": summary,
        "stats": {
            "total": len(records),
            "approved": len(approved),
            "pending": len(pending),
            "rejected": len(rejected),
            "refunded_amount": yuan(total_refunded),
            "refunded_cents": total_refunded,
            "locked_orders": len([o for o in my_orders
                                  if (locks.get(o) or {}).get("active")]),
        },
        "profile": _refund_profile(caller_id, records, mine),
    }


def _refund_profile(
    caller_id: str, records: list[dict[str, Any]], orders: list[dict[str, Any]]
) -> dict[str, Any]:
    """用户售后画像，供客服判断该用户是否属于高频退款人群。"""
    now = datetime.now()
    recent = 0
    for r in records:
        try:
            days = (now - datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")).days
        except (TypeError, ValueError):
            continue
        if days <= 30:
            recent += 1
    issue_counter: dict[str, int] = {}
    for r in records:
        key = r.get("issue_type", "OTHER")
        issue_counter[key] = issue_counter.get(key, 0) + 1
    top_issue = max(issue_counter.items(), key=lambda kv: kv[1])[0] if issue_counter else None
    return {
        "order_count": len(orders),
        "refund_count": len(records),
        "recent_30d": recent,
        "top_issue": rules.ISSUE_LABEL.get(top_issue, "无") if top_issue else "无",
        "frequent": recent >= 2,
    }


# --------------------------------------------------------------------------
# 售后数据分析
# --------------------------------------------------------------------------
ANALYTICS_DIMENSIONS = ("SUMMARY", "PRODUCT", "REASON", "ANOMALY", "TREND")


def sales_analytics(period: str = "ALL", dimension: str = "SUMMARY") -> dict[str, Any]:
    """售后数据聚合。dimension 决定返回哪一块明细，SUMMARY 返回全貌。"""
    dimension = (dimension or "SUMMARY").upper()
    if dimension not in ANALYTICS_DIMENSIONS:
        raise BizError(E_INVALID, f"不支持的分析维度 {dimension}",
                       f"可选值: {', '.join(ANALYTICS_DIMENSIONS)}")
    period = (period or "ALL").upper()
    if period not in analytics.PERIODS:
        raise BizError(E_INVALID, f"不支持的时间范围 {period}",
                       f"可选值: {', '.join(analytics.PERIODS)}")

    data = analytics.summary(period)
    payload: dict[str, Any] = {
        "ok": True,
        "period": data["period"],
        "totals": data["totals"],
        "text": analytics.to_text(data),
    }
    if dimension in ("SUMMARY", "PRODUCT"):
        payload["by_product"] = data["by_product"]
    if dimension in ("SUMMARY", "REASON"):
        payload["by_issue"] = data["by_issue"]
    if dimension in ("SUMMARY", "ANOMALY"):
        payload["anomalies"] = data["anomalies"]
    if dimension in ("SUMMARY", "TREND"):
        payload["trend"] = data["trend"]
    return payload


# --------------------------------------------------------------------------
# 取消
# --------------------------------------------------------------------------
def cancel_order(caller_id: str, order_id: str, reason: str | None = None) -> dict[str, Any]:
    order_id = (order_id or "").strip().upper()
    if not order_id:
        raise BizError(E_INVALID, "缺少订单号", "请先向用户索要订单号，不要自行编造")

    ts = now()
    with store.transaction(store.ORDERS_FILE, store.empty_orders()) as data:
        order = data["orders"].get(order_id)
        if order is None or order["owner_id"] != caller_id:
            raise BizError(E_NOT_FOUND, f"未找到订单 {order_id}",
                           "订单不存在或不属于当前用户，不要透露其是否存在")
        if order["status"] in ("CANCELLED", "REFUNDED") or order.get("cancelled_at"):
            raise BizError(E_ALREADY_CANCELLED, f"订单 {order_id} 已经取消过了，不能重复取消",
                           "每笔订单只能取消一次，直接把结果告知用户即可")
        if order["status"] == "COMPLETED":
            raise BizError(E_STATE, f"订单 {order_id} 已完成，无法取消",
                           "已签收订单请引导用户走退款申请流程")
        if order["status"] not in CANCELLABLE_STATUS:
            raise BizError(E_STATE, f"订单 {order_id} 当前状态为 "
                                    f"{STATUS_LABEL.get(order['status'])}，无法取消")

        order["status"] = "CANCELLED"
        order["cancelled_at"] = ts
        order["cancel_reason"] = reason or "OTHER"
        order["updated_at"] = ts
        snapshot = dict(order)

    refunds = store.read_only(store.REFUNDS_FILE, store.empty_refunds())
    return {
        "ok": True,
        "action": "cancel",
        "message": f"订单 {order_id} 已取消",
        "order": _shape(snapshot, refunds),
        "next_step": "如需退款，可继续发起退款（未发货直接退，已发货需商家审批）",
    }


# --------------------------------------------------------------------------
# 退款：规则引擎判定，自动放款 or 进人工队列
# --------------------------------------------------------------------------
def refund_order(
    caller_id: str,
    order_id: str,
    amount: float | None = None,
    reason: str | None = None,
    issue_type: str | None = None,
) -> dict[str, Any]:
    """兼容入口。amount 会被当作「用户主张金额」，最终金额仍由规则引擎裁决。"""
    return submit_refund(
        caller_id,
        order_id,
        reason=reason,
        issue_type=issue_type,
        requested_amount=amount,
    )


def submit_refund(
    caller_id: str,
    order_id: str,
    *,
    reason: str | None = None,
    issue_type: str | None = None,
    requested_amount: float | None = None,
    channel: str = audit.CHANNEL_SYSTEM,
    session_id: str | None = None,
) -> dict[str, Any]:
    """提交退款。模型只能提供诉求，金额与通道由 rules.evaluate 决定。

    锁顺序固定为 REFUNDS -> ORDERS。审批路径同样如此，避免两个进程
    一个先锁订单、一个先锁退款时互相等待（ABBA 死锁）。
    """
    order_id = (order_id or "").strip().upper()
    if not order_id:
        raise BizError(E_INVALID, "缺少订单号", "请先向用户索要订单号，不要自行编造")

    reason_text = (reason or "").strip() or "未填写退款原因"
    issue = issue_type if issue_type in rules.ISSUE_TYPES else "OTHER"
    requested_cents = _to_cents(requested_amount)

    orders = store.read_only(store.ORDERS_FILE, store.empty_orders())["orders"]
    order = orders.get(order_id)
    if order is None or order["owner_id"] != caller_id:
        raise BizError(E_NOT_FOUND, f"未找到订单 {order_id}",
                       "订单不存在或不属于当前用户，不要透露其是否存在")

    refunds_snap = store.read_only(store.REFUNDS_FILE, store.empty_refunds())
    if _lock_of(order_id, refunds_snap):
        raise BizError(
            E_REFUND_LOCKED,
            f"订单 {order_id} 的退款申请此前被商家驳回并锁定，暂无法再次提交",
            "不要重试。如实告知用户需联系人工客服复核，由商家解锁后才能重新申请。",
        )
    pending = _pending_refund(order_id, refunds_snap)
    if pending:
        raise BizError(
            E_PENDING_EXISTS,
            f"订单 {order_id} 已有一笔待审批的退款申请（{yuan(pending['amount_cents'])}）",
            f"申请编号 {pending['refund_id']}，告知用户正在商家审核中，请勿重复提交。",
        )

    # 已全额退完，不论当前状态如何都先报这个更准确的原因
    if order.get("refunded_cents", 0) >= order["amount_cents"]:
        raise BizError(
            E_ALREADY_REFUNDED,
            f"订单 {order_id} 已全额退款 {yuan(order['refunded_cents'])}，没有可退余额",
            "直接说明已退款金额和到账时间，不要再发起退款",
        )

    if order["status"] not in REFUNDABLE_STATUS:
        raise BizError(
            E_NOT_REFUNDABLE,
            f"订单 {order_id} 当前是{STATUS_LABEL[order['status']]}，无法申请退款",
            "已支付未取消的订单请先取消；未支付的订单无需退款",
        )

    user_refunds = [r for r in refunds_snap["refunds"].values()
                    if r["user_id"] == caller_id]
    decision = rules.evaluate(
        order,
        requested_cents=requested_cents,
        issue_type=issue,
        user_refunds=user_refunds,
    )

    if decision["decision"] == rules.DECISION_REJECT:
        balance = order["amount_cents"] - order.get("refunded_cents", 0)
        code = E_ALREADY_REFUNDED if decision.get("reject_code") == "NO_BALANCE" else E_AMOUNT
        raise BizError(
            code,
            decision.get("reject_reason") or "该申请不符合退款条件",
            f"本次最多可退 {yuan(balance)}，请调整金额或省略金额表示退剩余全部",
            extra={
                "refundable_balance": yuan(balance),
                "already_refunded": yuan(order.get("refunded_cents", 0)),
                "order_amount": yuan(order["amount_cents"]),
            },
        )

    audit.log(
        audit.KIND_RULE_DECISION,
        {
            "order_id": order_id,
            "decision": decision["decision"],
            "decision_label": decision["decision_label"],
            "risk_level": decision["risk_level"],
            "risk_score": decision["risk_score"],
            "priority": decision["priority"],
            "amount_cents": decision["amount_cents"],
            "amount_reason": decision["amount_reason"],
            "issue_type": issue,
            "rules": decision["rules"],
            "summary": decision["summary"],
        },
        session_id=session_id,
        user_id=caller_id,
        user_name=order["owner_name"],
        channel=channel,
    )

    return _write_refund(
        caller_id=caller_id, order=order, decision=decision,
        reason_text=reason_text, requested_cents=requested_cents,
    )


def _write_refund(
    *,
    caller_id: str,
    order: dict[str, Any],
    decision: dict[str, Any],
    reason_text: str,
    requested_cents: int | None,
) -> dict[str, Any]:
    """落库。自动放款与人工队列共用一段事务，锁顺序 REFUNDS -> ORDERS。"""
    order_id = order["order_id"]
    ts = now()
    refund_id = _new_refund_id()
    auto = decision["decision"] == rules.DECISION_AUTO
    cents = decision["amount_cents"]

    with store.transaction(store.REFUNDS_FILE, store.empty_refunds()) as rdata:
        # 进锁后再查一次，挡住并发重复提交
        if _pending_refund(order_id, rdata):
            raise BizError(E_PENDING_EXISTS, f"订单 {order_id} 已有一笔待审批的退款申请",
                           "告知用户正在审核中即可，不要重复提交")
        rdata["seq"] += 1
        seq = rdata["seq"]
        record = {
            "refund_id": refund_id,
            "order_id": order_id,
            "user_id": caller_id,
            "user_name": order["owner_name"],
            "amount_cents": cents,
            "requested_cents": requested_cents,
            "reason": reason_text,
            "issue_type": decision["issue_type"],
            "status": "APPROVED" if auto else "PENDING",
            "mode": "AUTO" if auto else "MANUAL",
            "seq": seq,
            "created_at": ts,
            "reviewed_at": ts if auto else None,
            "reviewer_id": "system" if auto else None,
            "reviewer_name": "系统自动放款" if auto else None,
            "review_note": "规则引擎自动放款，无需人工审批" if auto else None,
            "decision": {
                "decision": decision["decision"],
                "decision_label": decision["decision_label"],
                "risk_level": decision["risk_level"],
                "risk_label": decision["risk_label"],
                "risk_score": decision["risk_score"],
                "priority": decision["priority"],
                "amount_reason": decision["amount_reason"],
                "summary": decision["summary"],
                "rules": decision["rules"],
                "needs_evidence": decision["needs_evidence"],
            },
            "review_history": [],
        }

        if auto:
            with store.transaction(store.ORDERS_FILE, store.empty_orders()) as odata:
                live = odata["orders"].get(order_id)
                if live is None:
                    raise BizError(E_NOT_FOUND, "关联订单不存在", "")
                balance = live["amount_cents"] - live.get("refunded_cents", 0)
                if cents > balance:
                    raise BizError(
                        E_AMOUNT,
                        f"可退余额仅剩 {yuan(balance)}，不足以支付本次 {yuan(cents)}",
                        "请让用户按剩余额度重新提交",
                    )
                _apply_refund(live, cents, ts)
                snapshot = dict(live)

        rdata["refunds"][refund_id] = record
        position = None if auto else _queue_position(rdata, refund_id)

    refunds = store.read_only(store.REFUNDS_FILE, store.empty_refunds())
    if auto:
        return {
            "ok": True,
            "action": "refund",
            "result": "REFUNDED",
            "refund_id": refund_id,
            "amount": yuan(cents),
            "decision": {
                "channel": "AUTO_APPROVE",
                "risk_label": decision["risk_label"],
                "risk_score": decision["risk_score"],
                "amount_reason": decision["amount_reason"],
                "summary": decision["summary"],
            },
            "message": f"订单 {order_id} 退款 {yuan(cents)} 已放款（{decision['issue_label']}，规则引擎自动通过）",
            "order": _shape(snapshot, refunds),
            "arrival_note": "原路退回，通常 1-3 个工作日到账",
        }

    return {
        "ok": True,
        "action": "refund_submitted",
        "result": "PENDING",
        "refund_id": refund_id,
        "amount": yuan(cents),
        "queue_position": position,
        "decision": {
            "channel": "MANUAL_REVIEW",
            "risk_label": decision["risk_label"],
            "risk_score": decision["risk_score"],
            "priority": decision["priority"],
            "amount_reason": decision["amount_reason"],
            "summary": decision["summary"],
            "rules": [{"id": r["id"], "name": r["name"], "delta": r["delta"]}
                      for r in decision["rules"]],
        },
        "message": (
            f"订单 {order_id} 的退款申请（{yuan(cents)}）已提交，"
            f"当前排队第 {position} 位，等待商家审批"
        ),
        "order": _shape(order, refunds),
        "next_step": "商家审批通过后放款，可让用户凭申请编号查询进度",
    }


def _apply_refund(order: dict[str, Any], cents: int, ts: str) -> None:
    """把一笔退款累加到订单上，退满则置为 REFUNDED。"""
    order["refunded_cents"] = order.get("refunded_cents", 0) + cents
    order["refunded_at"] = ts
    order["updated_at"] = ts
    if order["refunded_cents"] >= order["amount_cents"]:
        order["status"] = "REFUNDED"


def _queue_position(rdata: dict[str, Any], refund_id: str) -> int:
    pending = sorted(
        (r for r in rdata["refunds"].values() if r["status"] == "PENDING"),
        key=lambda r: r["seq"],
    )
    for idx, r in enumerate(pending, start=1):
        if r["refund_id"] == refund_id:
            return idx
    return len(pending)


# --------------------------------------------------------------------------
# 商家审批
# --------------------------------------------------------------------------
def _require_merchant(reviewer_id: str) -> dict[str, Any]:
    user = get_user(reviewer_id)
    if user["role"] != "merchant":
        raise BizError(E_FORBIDDEN, "只有商家账号可以审批退款", "请使用商家账号登录后台")
    return user


def approve_refund(refund_id: str, reviewer_id: str, note: str = "") -> dict[str, Any]:
    """审批通过：先占用申请（防重复审批），再把金额累加到订单。"""
    reviewer = _require_merchant(reviewer_id)
    ts = now()

    with store.transaction(store.REFUNDS_FILE, store.empty_refunds()) as rdata:
        refund = rdata["refunds"].get(refund_id)
        if refund is None:
            raise BizError(E_REFUND_NOT_FOUND, f"未找到退款申请 {refund_id}", "")
        if refund["status"] != "PENDING":
            raise BizError(
                E_REFUND_HANDLED,
                f"该申请已{REFUND_STATUS_LABEL.get(refund['status'], refund['status'])}，不能重复处理",
                "刷新列表查看最新状态",
            )

        with store.transaction(store.ORDERS_FILE, store.empty_orders()) as odata:
            order = odata["orders"].get(refund["order_id"])
            if order is None:
                raise BizError(E_NOT_FOUND, "关联订单不存在", "")
            # 审批期间其它申请可能已经消耗了额度，这里必须再校验一次
            balance = order["amount_cents"] - order.get("refunded_cents", 0)
            if refund["amount_cents"] > balance:
                raise BizError(
                    E_AMOUNT,
                    f"可退余额仅剩 {yuan(balance)}，不足以支付本次申请 "
                    f"{yuan(refund['amount_cents'])}",
                    "请驳回该申请，让用户按剩余额度重新提交",
                )
            _apply_refund(order, refund["amount_cents"], ts)
            order_snapshot = dict(order)

        refund["status"] = "APPROVED"
        refund["reviewed_at"] = ts
        refund["reviewer_id"] = reviewer["user_id"]
        refund["reviewer_name"] = reviewer["name"]
        refund["review_note"] = note or "审批通过"
        refund.setdefault("review_history", []).append({
            "action": "APPROVE",
            "action_label": "审批通过",
            "at": ts,
            "by_id": reviewer["user_id"],
            "by": reviewer["name"],
            "note": note or "审批通过",
            "amount_cents": refund["amount_cents"],
        })
        rdata.setdefault("locks", {}).pop(refund["order_id"], None)
        review_snapshot = dict(refund)

    audit.log(
        audit.KIND_HUMAN_ACTION,
        {
            "action": "APPROVE",
            "action_label": "审批通过",
            "refund_id": refund_id,
            "order_id": review_snapshot["order_id"],
            "amount_cents": review_snapshot["amount_cents"],
            "note": review_snapshot["review_note"],
            "agent_decision": (review_snapshot.get("decision") or {}).get("summary"),
        },
        user_id=reviewer["user_id"], user_name=reviewer["name"],
        channel=audit.CHANNEL_WEB,
    )

    refunds = store.read_only(store.REFUNDS_FILE, store.empty_refunds())
    return {
        "ok": True,
        "refund_id": refund_id,
        "message": f"退款 {yuan(refund['amount_cents'])} 已通过并放款",
        "order": _shape(order_snapshot, refunds),
    }


def reject_refund(refund_id: str, reviewer_id: str, note: str = "") -> dict[str, Any]:
    """驳回：申请置 REJECTED，并给订单上锁，用户不能自行重提。"""
    reviewer = _require_merchant(reviewer_id)
    ts = now()
    note_text = note or "商家驳回，未说明原因"

    with store.transaction(store.REFUNDS_FILE, store.empty_refunds()) as rdata:
        refund = rdata["refunds"].get(refund_id)
        if refund is None:
            raise BizError(E_REFUND_NOT_FOUND, f"未找到退款申请 {refund_id}", "")
        if refund["status"] != "PENDING":
            raise BizError(
                E_REFUND_HANDLED,
                f"该申请已{REFUND_STATUS_LABEL.get(refund['status'], refund['status'])}，不能重复处理",
                "刷新列表查看最新状态",
            )

        refund["status"] = "REJECTED"
        refund["reviewed_at"] = ts
        refund["reviewer_id"] = reviewer["user_id"]
        refund["reviewer_name"] = reviewer["name"]
        refund["review_note"] = note_text
        refund.setdefault("review_history", []).append({
            "action": "REJECT",
            "action_label": "驳回申请",
            "at": ts,
            "by_id": reviewer["user_id"],
            "by": reviewer["name"],
            "note": note_text,
            "amount_cents": refund["amount_cents"],
        })

        rdata.setdefault("locks", {})[refund["order_id"]] = {
            "order_id": refund["order_id"],
            "refund_id": refund_id,
            "locked_at": ts,
            "reason": note_text,
            "locked_by": reviewer["name"],
            "active": True,
        }
        review_snapshot = dict(refund)

    audit.log(
        audit.KIND_HUMAN_ACTION,
        {
            "action": "REJECT",
            "action_label": "驳回申请",
            "refund_id": refund_id,
            "order_id": review_snapshot["order_id"],
            "amount_cents": review_snapshot["amount_cents"],
            "note": note_text,
            "agent_decision": (review_snapshot.get("decision") or {}).get("summary"),
        },
        user_id=reviewer["user_id"], user_name=reviewer["name"],
        channel=audit.CHANNEL_WEB,
    )

    return {
        "ok": True,
        "refund_id": refund_id,
        "message": "申请已驳回，该订单进入退款锁定状态",
    }


def unlock_order(order_id: str, operator_id: str, note: str = "") -> dict[str, Any]:
    """运营解锁：解除驳回锁，允许用户重新申请。"""
    operator = _require_merchant(operator_id)
    order_id = (order_id or "").strip().upper()
    ts = now()

    with store.transaction(store.REFUNDS_FILE, store.empty_refunds()) as rdata:
        lock = rdata.get("locks", {}).get(order_id)
        if not lock or not lock.get("active"):
            return {
                "ok": True,
                "changed": False,
                "message": f"订单 {order_id} 当前没有生效中的退款锁",
            }
        lock["active"] = False
        lock["unlocked_at"] = ts
        lock["unlocked_by"] = operator["name"]
        lock["unlock_note"] = note or "运营人工解锁"
        lock_note = lock["unlock_note"]

    audit.log(
        audit.KIND_HUMAN_ACTION,
        {
            "action": "UNLOCK",
            "action_label": "解除退款锁",
            "order_id": order_id,
            "note": lock_note,
        },
        user_id=operator["user_id"], user_name=operator["name"],
        channel=audit.CHANNEL_WEB,
    )

    return {
        "ok": True,
        "changed": True,
        "message": f"订单 {order_id} 已解锁，用户可重新申请退款",
    }


# --------------------------------------------------------------------------
# 退款查询
# --------------------------------------------------------------------------
def list_refunds(
    order_id: str | None = None,
    user_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    data = store.read_only(store.REFUNDS_FILE, store.empty_refunds())
    orders = store.read_only(store.ORDERS_FILE, store.empty_orders())["orders"]

    rows = list(data["refunds"].values())
    if order_id:
        rows = [r for r in rows if r["order_id"] == order_id.strip().upper()]
    if user_id:
        rows = [r for r in rows if r["user_id"] == user_id]
    if status:
        rows = [r for r in rows if r["status"] == status]

    rows.sort(key=lambda r: r["seq"], reverse=True)
    limit = max(1, min(int(limit or 50), 200))

    out = []
    for r in rows[:limit]:
        order = orders.get(r["order_id"], {})
        order_amount = order.get("amount_cents", 0)
        order_refunded = order.get("refunded_cents", 0)
        pending = r["amount_cents"] if r["status"] == "PENDING" else 0
        decision = r.get("decision") or {}
        out.append({
            "refund_id": r["refund_id"],
            "order_id": r["order_id"],
            "user_id": r["user_id"],
            "user_name": r["user_name"],
            "amount": yuan(r["amount_cents"]),
            "amount_cents": r["amount_cents"],
            "requested_amount": yuan(r.get("requested_cents")
                                     if r.get("requested_cents") is not None
                                     else r["amount_cents"]),
            "order_amount": yuan(order_amount),
            "order_amount_cents": order_amount,
            "order_refunded": yuan(order_refunded),
            "order_refunded_cents": order_refunded,
            "balance_after": yuan(order_amount - order_refunded - pending),
            "item_name": order.get("item_name", "—"),
            "shipping_state": (
                "DELIVERED" if order.get("delivered_at")
                else ("SHIPPED" if order.get("shipped_at") else "NOT_SHIPPED")
            ),
            "reason": r["reason"],
            "issue_type": r.get("issue_type", "OTHER"),
            "issue_label": rules.ISSUE_LABEL.get(r.get("issue_type", "OTHER"), "其他原因"),
            "status": r["status"],
            "status_label": REFUND_STATUS_LABEL[r["status"]],
            "mode": r["mode"],
            "seq": r["seq"],
            "created_at": r["created_at"],
            "reviewed_at": r["reviewed_at"],
            "reviewer_name": r["reviewer_name"],
            "review_note": r["review_note"],
            # 规则引擎判定，供审核中心展示
            "decision": decision.get("decision"),
            "decision_label": decision.get("decision_label"),
            "risk_level": decision.get("risk_level"),
            "risk_label": decision.get("risk_label"),
            "risk_score": decision.get("risk_score"),
            "priority": decision.get("priority"),
            "decision_summary": decision.get("summary"),
            "needs_evidence": decision.get("needs_evidence", False),
        })
    return out


def review_detail(refund_id: str) -> dict[str, Any]:
    """审核详情：订单全貌 + 该单全部退款历史 + 用户历史 + Agent 判断依据。"""
    data = store.read_only(store.REFUNDS_FILE, store.empty_refunds())
    refund = data["refunds"].get(refund_id)
    if refund is None:
        raise BizError(E_REFUND_NOT_FOUND, f"未找到退款申请 {refund_id}", "")

    orders = store.read_only(store.ORDERS_FILE, store.empty_orders())["orders"]
    order = orders.get(refund["order_id"], {})
    if not order:
        raise BizError(E_NOT_FOUND, "关联订单不存在", "")

    history = [
        r for r in data["refunds"].values() if r["order_id"] == refund["order_id"]
    ]
    history.sort(key=lambda r: r["seq"])

    user_history = [
        r for r in data["refunds"].values() if r["user_id"] == refund["user_id"]
    ]
    user_history.sort(key=lambda r: r["seq"], reverse=True)

    decision = refund.get("decision") or {}
    lock = (data.get("locks", {}).get(refund["order_id"]) or {})

    return {
        "refund": {
            "refund_id": refund["refund_id"],
            "status": refund["status"],
            "status_label": REFUND_STATUS_LABEL[refund["status"]],
            "amount": yuan(refund["amount_cents"]),
            "amount_cents": refund["amount_cents"],
            "requested_amount": yuan(refund.get("requested_cents")
                                     if refund.get("requested_cents") is not None
                                     else refund["amount_cents"]),
            "requested_cents": refund.get("requested_cents"),
            "reason": refund["reason"],
            "issue_type": refund.get("issue_type", "OTHER"),
            "issue_label": rules.ISSUE_LABEL.get(refund.get("issue_type", "OTHER"), "其他原因"),
            "mode": refund.get("mode"),
            "created_at": refund["created_at"],
            "reviewed_at": refund.get("reviewed_at"),
            "reviewer_name": refund.get("reviewer_name"),
            "review_note": refund.get("review_note"),
            "review_history": refund.get("review_history") or [],
        },
        "order": _shape(order, data),
        "decision": {
            "decision": decision.get("decision"),
            "decision_label": decision.get("decision_label"),
            "risk_level": decision.get("risk_level"),
            "risk_label": decision.get("risk_label"),
            "risk_score": decision.get("risk_score"),
            "priority": decision.get("priority"),
            "amount_reason": decision.get("amount_reason"),
            "summary": decision.get("summary"),
            "needs_evidence": decision.get("needs_evidence"),
            "rules": decision.get("rules") or [],
        },
        "order_history": [_brief(r) for r in history],
        "user_history": [_brief(r) for r in user_history[:10]],
        "lock": lock if lock.get("active") else None,
        "user": {
            "user_id": refund["user_id"],
            "user_name": refund["user_name"],
            "refund_count": len(user_history),
            "approved_count": len([r for r in user_history if r["status"] == "APPROVED"]),
        },
    }


def _brief(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "refund_id": r["refund_id"],
        "order_id": r["order_id"],
        "amount": yuan(r["amount_cents"]),
        "status": r["status"],
        "status_label": REFUND_STATUS_LABEL[r["status"]],
        "mode": r.get("mode"),
        "issue_label": rules.ISSUE_LABEL.get(r.get("issue_type", "OTHER"), "其他原因"),
        "reason": r["reason"],
        "created_at": r["created_at"],
        "reviewer_name": r.get("reviewer_name"),
        "review_note": r.get("review_note"),
    }


def pending_queue(limit: int = 100) -> list[dict[str, Any]]:
    """商家待审批队列，按提交顺序排队，带排队位置与等待时长。"""
    rows = list_refunds(status="PENDING", limit=limit)
    rows.sort(key=lambda r: r["seq"])
    for idx, r in enumerate(rows, start=1):
        r["queue_position"] = idx
        r["waiting"] = _human_delta(r["created_at"])
    return rows


def _human_delta(since: str) -> str:
    try:
        start = datetime.strptime(since, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "—"
    seconds = max(0, int((datetime.now() - start).total_seconds()))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    if seconds < 86400:
        return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分"
    return f"{seconds // 86400} 天 {seconds % 86400 // 3600} 小时"


def dashboard_stats() -> dict[str, Any]:
    """后台顶部统计，含一个轻量的分析口径摘要。"""
    refunds = list_refunds(limit=200)
    pending = [r for r in refunds if r["status"] == "PENDING"]
    orders = store.read_only(store.ORDERS_FILE, store.empty_orders())["orders"]
    today = datetime.now().strftime("%Y-%m-%d")
    locks = store.read_only(store.REFUNDS_FILE, store.empty_refunds()).get("locks", {})

    month = analytics.summary("MONTH")["totals"]
    return {
        "pending_count": len(pending),
        "pending_amount": yuan(sum(r["amount_cents"] for r in pending)),
        "approved_today": len([
            r for r in refunds
            if r["status"] == "APPROVED" and (r["reviewed_at"] or "").startswith(today)
        ]),
        "rejected_count": len([r for r in refunds if r["status"] == "REJECTED"]),
        "locked_count": len([lk for lk in locks.values() if lk.get("active")]),
        "order_count": len(orders),
        # 分析口径
        "month_refund_amount": month["refund_amount"],
        "month_refund_rate": month["refund_rate"],
        "month_refund_count": month["refund_count"],
        "high_risk_pending": len([r for r in pending if r.get("risk_level") == "HIGH"]),
    }


def list_locks() -> list[dict[str, Any]]:
    data = store.read_only(store.REFUNDS_FILE, store.empty_refunds())
    rows = [lk for lk in data.get("locks", {}).values() if lk.get("active")]
    rows.sort(key=lambda r: r["locked_at"], reverse=True)
    return rows


def get_order_for_merchant(order_id: str) -> dict[str, Any]:
    """商家视角查单：不受 owner 限制，只读。"""
    orders = store.read_only(store.ORDERS_FILE, store.empty_orders())["orders"]
    order = orders.get((order_id or "").strip().upper())
    if order is None:
        raise BizError(E_NOT_FOUND, f"未找到订单 {order_id}", "")
    refunds = store.read_only(store.REFUNDS_FILE, store.empty_refunds())
    return _shape(order, refunds)
