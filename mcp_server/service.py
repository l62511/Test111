"""行级访问与业务规则。

设计要点：
1. 所有查询都强制带 owner_id 条件，越权订单统一表现为「不存在」，
   不区分「不是你的」和「没有这单」，避免被用来探测订单是否存在。
2. 写操作是一条带全部前置条件的 UPDATE，靠 rowcount 判定成败，
   不做「先查再改」，避免并发下重复写入。
3. operation_log 上的 UNIQUE(order_id, action) 是最后一道兜底。
4. 已发货订单的退款必须走人工审批：先开一张 PENDING 的退款追踪单，
   审批通过后凭 approval_id 二次调用才真正放款。审批金额以追踪单为准，
   不接受二次调用时传进来的新金额，防止绕过审批改价。
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = BASE_DIR / "data" / "orders.db"

VALID_STATUS = ("CREATED", "PAID", "CANCELLED", "REFUNDED", "COMPLETED")
CANCELLABLE_STATUS = ("CREATED", "PAID")

# 错误码，Agent 会据此生成面向用户的话术
E_NOT_FOUND = "ORDER_NOT_FOUND"
E_ALREADY_CANCELLED = "ALREADY_CANCELLED"
E_ALREADY_REFUNDED = "ALREADY_REFUNDED"
E_NOT_CANCELLED = "ORDER_NOT_CANCELLED"
E_STATE = "INVALID_STATE"
E_AMOUNT = "AMOUNT_EXCEEDED"
E_INVALID = "INVALID_ARGUMENT"
E_UNKNOWN_USER = "UNKNOWN_CALLER"
E_APPROVAL_REQUIRED = "MANUAL_APPROVAL_REQUIRED"
E_APPROVAL_INVALID = "INVALID_APPROVAL"
E_REFUND_LOCKED = "REFUND_LOCKED_AFTER_REJECT"


class BizError(Exception):
    def __init__(self, code: str, message: str, hint: str = "",
                 extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.extra = extra or {}


def db_path() -> Path:
    raw = os.getenv("ORDER_DB_PATH", "").strip()
    if not raw:
        return DEFAULT_DB
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path(), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def yuan(cents: int | None) -> str:
    return "—" if cents is None else f"¥{cents / 100:,.2f}"


def resolve_caller(caller_id: str) -> dict[str, str]:
    """校验身份真实存在。调用方身份由进程环境注入，不接受模型传参。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT DISTINCT owner_id, owner_name FROM orders WHERE owner_id = ?",
            (caller_id,),
        ).fetchone()
    if row is None:
        raise BizError(E_UNKNOWN_USER, f"未知的调用方身份 {caller_id}",
                       "请检查 CALLER_ID 配置是否对应真实用户")
    return {"caller_id": row["owner_id"], "caller_name": row["owner_name"]}


def _shape(row: sqlite3.Row, locked: bool = False) -> dict[str, Any]:
    shipped = row["shipped_at"] is not None
    can_refund = row["status"] == "CANCELLED" and row["refunded_at"] is None
    return {
        "order_id": row["order_id"],
        "item_name": row["item_name"],
        "item_desc": row["item_desc"],
        "amount": yuan(row["amount_cents"]),
        "amount_cents": row["amount_cents"],
        "status": row["status"],
        "status_label": STATUS_LABEL[row["status"]],
        "created_at": row["created_at"],
        "paid_at": row["paid_at"],
        "shipped": shipped,
        "shipped_at": row["shipped_at"],
        "shipping_state": "SHIPPED" if shipped else "NOT_SHIPPED",
        "cancelled_at": row["cancelled_at"],
        "refunded_at": row["refunded_at"],
        "refund_amount": yuan(row["refund_cents"]) if row["refund_cents"] else None,
        "tracking_no": row["tracking_no"],
        "can_cancel": row["status"] in CANCELLABLE_STATUS and row["cancelled_at"] is None,
        "can_refund": can_refund,
        # 已发货的退款要人工审批，未发货的直接退
        "refund_needs_approval": bool(shipped and can_refund),
        # 曾被人工驳回，需运营解锁后才能再申请
        "refund_locked": bool(locked and can_refund),
    }


STATUS_LABEL = {
    "CREATED": "待支付",
    "PAID": "已支付",
    "CANCELLED": "已取消",
    "REFUNDED": "已退款",
    "COMPLETED": "已完成",
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

    sql = ("SELECT * FROM orders WHERE owner_id = ?")
    args: list[Any] = [caller_id]
    if order_id:
        sql += " AND order_id = ?"
        args.append(order_id)
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)

    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
        locked_ids = _locked_order_ids(conn, [r["order_id"] for r in rows])

    if order_id and not rows:
        raise BizError(
            E_NOT_FOUND,
            f"未找到订单 {order_id}",
            "该订单不存在，或不属于当前登录用户。不要向用户确认它是否属于他人。",
        )

    orders = [_shape(r, r["order_id"] in locked_ids) for r in rows]
    return {
        "ok": True,
        "count": len(orders),
        "orders": orders,
        "summary": _summary_line(orders),
    }


def _locked_order_ids(
    conn: sqlite3.Connection, order_ids: list[str] | None = None
) -> set[str]:
    """当前处于「驳回锁定」状态、尚未被人工解锁的订单集合。"""
    sql = "SELECT DISTINCT order_id FROM refund_lock WHERE unlocked_at IS NULL"
    args: list[Any] = []
    if order_ids:
        sql += f" AND order_id IN ({','.join('?' * len(order_ids))})"
        args = list(order_ids)
    return {r["order_id"] for r in conn.execute(sql, args).fetchall()}


def _is_refund_locked(conn: sqlite3.Connection, order_id: str) -> bool:
    return order_id in _locked_order_ids(conn, [order_id])


def _summary_line(orders: list[dict[str, Any]]) -> str:
    if not orders:
        return "没有符合条件的订单"
    if len(orders) == 1:
        o = orders[0]
        return f"{o['order_id']} {o['item_name']} {o['amount']} {o['status_label']}"
    return f"共 {len(orders)} 笔订单"


# --------------------------------------------------------------------------
# 取消
# --------------------------------------------------------------------------
def cancel_order(caller_id: str, order_id: str, reason: str | None = None) -> dict[str, Any]:
    order_id = (order_id or "").strip().upper()
    if not order_id:
        raise BizError(E_INVALID, "缺少订单号", "请先向用户索要订单号，不要自行编造")

    ts = now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "UPDATE orders SET status='CANCELLED', cancelled_at=?, cancel_reason=?"
                " WHERE order_id=? AND owner_id=?"
                "   AND status IN ('CREATED','PAID') AND cancelled_at IS NULL",
                (ts, reason or "OTHER", order_id, caller_id),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                raise _explain_cancel_failure(conn, caller_id, order_id)

            conn.execute(
                "INSERT INTO operation_log (order_id, action, actor_id, detail, created_at)"
                " VALUES (?,?,?,?,?)",
                (order_id, "cancel", caller_id, reason or "OTHER", ts),
            )
            conn.execute("COMMIT")
        except BizError:
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise

        row = conn.execute(
            "SELECT * FROM orders WHERE order_id=? AND owner_id=?", (order_id, caller_id)
        ).fetchone()

    return {
        "ok": True,
        "action": "cancel",
        "message": f"订单 {order_id} 已取消",
        "order": _shape(row),
        "next_step": "如需退款，可继续发起退款（仅限已取消订单，且只能退一次）",
    }


def _explain_cancel_failure(conn: sqlite3.Connection, caller_id: str, order_id: str) -> BizError:
    """UPDATE 未命中时，回查原因，给出精确错误码。"""
    row = conn.execute(
        "SELECT status, cancelled_at FROM orders WHERE order_id=? AND owner_id=?",
        (order_id, caller_id),
    ).fetchone()
    if row is None:
        return BizError(E_NOT_FOUND, f"未找到订单 {order_id}",
                        "订单不存在或不属于当前用户，不要透露其是否存在")
    if row["cancelled_at"] is not None or row["status"] in ("CANCELLED", "REFUNDED"):
        return BizError(E_ALREADY_CANCELLED, f"订单 {order_id} 已经取消过了，不能重复取消",
                        "每笔订单只能取消一次，直接把结果告知用户即可")
    if row["status"] == "COMPLETED":
        return BizError(E_STATE, f"订单 {order_id} 已完成，无法取消",
                        "已签收订单请引导用户走售后流程")
    return BizError(E_STATE, f"订单 {order_id} 当前状态为 {STATUS_LABEL.get(row['status'])}, 无法取消")


# --------------------------------------------------------------------------
# 退款
# --------------------------------------------------------------------------
def refund_order(
    caller_id: str,
    order_id: str,
    amount: float | None = None,
    reason: str | None = None,
    approval_id: str | None = None,
) -> dict[str, Any]:
    """退款。

    未发货 -> 直接退，trace 记为 AUTO / NOT_REQUIRED。
    已发货 -> 先开审批单，第二次带 approval_id 调用才真正放款，
               trace 记为 MANUAL / PENDING -> APPROVED。
    """
    order_id = (order_id or "").strip().upper()
    if not order_id:
        raise BizError(E_INVALID, "缺少订单号", "请先向用户索要订单号，不要自行编造")

    reason_text = (reason or "").strip() or "未填写退款原因"

    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE order_id=? AND owner_id=?", (order_id, caller_id)
        ).fetchone()
        if row is None:
            raise BizError(E_NOT_FOUND, f"未找到订单 {order_id}",
                           "订单不存在或不属于当前用户，不要透露其是否存在")
        if row["refunded_at"] is not None or row["status"] == "REFUNDED":
            raise BizError(E_ALREADY_REFUNDED, f"订单 {order_id} 已退款，不能重复退款",
                           "每笔订单只能退一次，直接说明到账时间即可")
        if row["status"] != "CANCELLED":
            raise BizError(
                E_NOT_CANCELLED,
                f"订单 {order_id} 当前是{STATUS_LABEL[row['status']]}，只有已取消的订单可以退款",
                "请先调用 cancel_order 取消该订单，再发起退款",
            )

        # 曾被人工驳回 -> 锁定，不允许用户自行再申请
        if _is_refund_locked(conn, order_id):
            raise BizError(
                E_REFUND_LOCKED,
                f"订单 {order_id} 的退款申请此前被人工驳回，现已锁定，无法再次提交",
                "不要重试、不要换说法再调一次。如实告知用户需联系人工客服复核，"
                "由运营解锁后才能重新申请。",
            )

        shipped = row["shipped_at"] is not None
        if shipped:
            return _refund_shipped(conn, caller_id, row, amount, reason_text, approval_id)
        return _refund_unshipped(conn, caller_id, row, amount, reason_text)


def _resolve_amount(row: sqlite3.Row, amount: float | None) -> int:
    refund_cents = row["amount_cents"] if amount is None else int(round(float(amount) * 100))
    if refund_cents <= 0:
        raise BizError(E_AMOUNT, "退款金额必须大于 0")
    if refund_cents > row["amount_cents"]:
        raise BizError(
            E_AMOUNT,
            f"退款金额 {yuan(refund_cents)} 超过实付金额 {yuan(row['amount_cents'])}",
            "请把金额调到实付金额以内，或省略金额表示全额退款",
        )
    return refund_cents


# --------------------------------------------------------------------------
# 未发货：直接退，无需审批
# --------------------------------------------------------------------------
def _refund_unshipped(
    conn: sqlite3.Connection,
    caller_id: str,
    row: sqlite3.Row,
    amount: float | None,
    reason: str,
) -> dict[str, Any]:
    order_id = row["order_id"]
    refund_cents = _resolve_amount(row, amount)
    ts = now()
    trace_id = _new_trace_id()

    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "UPDATE orders SET status='REFUNDED', refunded_at=?, refund_cents=?"
            " WHERE order_id=? AND owner_id=? AND status='CANCELLED'"
            "   AND refunded_at IS NULL AND refund_cents IS NULL",
            (ts, refund_cents, order_id, caller_id),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            raise BizError(E_ALREADY_REFUNDED, f"订单 {order_id} 已退款或状态已变更",
                           "请重新查询订单状态后再决定")

        conn.execute(
            "INSERT INTO operation_log (order_id, action, actor_id, detail, created_at)"
            " VALUES (?,?,?,?,?)",
            (order_id, "refund", caller_id, f"{yuan(refund_cents)} {reason}", ts),
        )
        conn.execute(
            "INSERT INTO refund_trace (trace_id, order_id, caller_id, amount_cents, reason,"
            " shipped, shipping_state, approval_mode, approval_status, approver, outcome,"
            " note, created_at, updated_at)"
            " VALUES (?,?,?,?,?,0,'NOT_SHIPPED','AUTO','NOT_REQUIRED',NULL,'SUCCESS',?,?,?)",
            (trace_id, order_id, caller_id, refund_cents, reason,
             "未发货订单，系统自动放款", ts, ts),
        )
        conn.execute("COMMIT")
    except BizError:
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise

    updated = conn.execute(
        "SELECT * FROM orders WHERE order_id=? AND owner_id=?", (order_id, caller_id)
    ).fetchone()

    return {
        "ok": True,
        "action": "refund",
        "approval": "NOT_REQUIRED",
        "trace_id": trace_id,
        "message": f"订单 {order_id} 退款 {yuan(refund_cents)} 已提交（未发货，无需审批）",
        "order": _shape(updated),
        "arrival_note": "原路退回，通常 1-3 个工作日到账",
    }


# --------------------------------------------------------------------------
# 已发货：开审批单，审批通过后凭 approval_id 二次调用放款
# --------------------------------------------------------------------------
def _refund_shipped(
    conn: sqlite3.Connection,
    caller_id: str,
    row: sqlite3.Row,
    amount: float | None,
    reason: str,
    approval_id: str | None,
) -> dict[str, Any]:
    order_id = row["order_id"]

    # ---- 第一次调用：只开单，不放款 ----
    if not approval_id:
        refund_cents = _resolve_amount(row, amount)
        ts = now()
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT trace_id FROM refund_trace"
                " WHERE order_id=? AND caller_id=? AND approval_status='PENDING'"
                " ORDER BY created_at DESC LIMIT 1",
                (order_id, caller_id),
            ).fetchone()
            if existing:
                trace_id = existing["trace_id"]
            else:
                trace_id = _new_trace_id()
                conn.execute(
                    "INSERT INTO refund_trace (trace_id, order_id, caller_id, amount_cents,"
                    " reason, shipped, shipping_state, approval_mode, approval_status,"
                    " approver, outcome, note, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,1,'SHIPPED','MANUAL','PENDING',NULL,'PENDING',?,?,?)",
                    (trace_id, order_id, caller_id, refund_cents, reason,
                     "已发货件，等待人工审批", ts, ts),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        raise BizError(
            E_APPROVAL_REQUIRED,
            f"订单 {order_id} 已发货，退款需要人工审批",
            f"请向用户说明该订单已发货，需人工审批后退款（金额 {yuan(refund_cents)}）。"
            f"用户同意后，带 approval_id 再次调用 refund_order 完成放款。",
            extra={
                "approval_id": trace_id,
                "amount": yuan(refund_cents),
                "shipping_state": "SHIPPED",
                "tracking_no": row["tracking_no"],
                "reason": reason,
            },
        )

    # ---- 第二次调用：校验审批单后放款 ----
    trace = conn.execute(
        "SELECT * FROM refund_trace WHERE trace_id=?", (approval_id,)
    ).fetchone()
    if trace is None or trace["order_id"] != order_id or trace["caller_id"] != caller_id:
        raise BizError(E_APPROVAL_INVALID, "审批凭证无效",
                       "用户拒绝或凭证不存在时，不要重试，如实告知操作未执行")

    if trace["approval_status"] == "REJECTED":
        raise BizError(E_APPROVAL_INVALID, "该退款申请已被拒绝，不能再执行",
                       "如实告知用户审批未通过，不要重复尝试")
    if trace["approval_status"] == "APPROVED":
        raise BizError(E_ALREADY_REFUNDED, "该审批单已经执行过退款",
                       "不要重复放款")
    if trace["approval_status"] != "PENDING":
        raise BizError(E_APPROVAL_INVALID, "审批单状态异常，无法执行")

    # 关键：金额以审批单为准，不接受本次调用传入的新金额，防止审批后改价
    refund_cents = trace["amount_cents"]
    approver = f"{row['owner_name']}（终端人工确认）"
    ts = now()

    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "UPDATE orders SET status='REFUNDED', refunded_at=?, refund_cents=?"
            " WHERE order_id=? AND owner_id=? AND status='CANCELLED'"
            "   AND refunded_at IS NULL AND refund_cents IS NULL",
            (ts, refund_cents, order_id, caller_id),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            raise BizError(E_ALREADY_REFUNDED, f"订单 {order_id} 已退款或状态已变更",
                           "请重新查询订单状态后再决定")

        cur = conn.execute(
            "UPDATE refund_trace SET approval_status='APPROVED', approver=?,"
            " outcome='SUCCESS', updated_at=?"
            " WHERE trace_id=? AND approval_status='PENDING'",
            (approver, ts, approval_id),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            raise BizError(E_APPROVAL_INVALID, "审批单已被处理，退款未执行",
                           "请勿重复提交")

        conn.execute(
            "INSERT INTO operation_log (order_id, action, actor_id, detail, created_at)"
            " VALUES (?,?,?,?,?)",
            (order_id, "refund", caller_id, f"{yuan(refund_cents)} {trace['reason']}", ts),
        )
        conn.execute("COMMIT")
    except BizError:
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise

    updated = conn.execute(
        "SELECT * FROM orders WHERE order_id=? AND owner_id=?", (order_id, caller_id)
    ).fetchone()

    return {
        "ok": True,
        "action": "refund",
        "approval": "APPROVED",
        "trace_id": approval_id,
        "message": f"订单 {order_id} 退款 {yuan(refund_cents)} 已通过人工审批并放款",
        "order": _shape(updated),
        "arrival_note": "原路退回，通常 1-3 个工作日到账",
        "reason": trace["reason"],
    }


def reject_refund(approval_id: str, approver: str, note: str = "") -> bool:
    """审批未通过：关闭追踪单，并把该订单锁住，用户不能自行再申请。

    由交互层在用户驳回后调用。锁定与状态更新在同一事务里，
    避免出现「trace 记了驳回但没上锁」的半截状态。
    """
    if not approval_id:
        return False
    ts = now()
    note_text = note or "人工审批未通过"

    with connect() as conn:
        trace = conn.execute(
            "SELECT order_id FROM refund_trace WHERE trace_id=?", (approval_id,)
        ).fetchone()
        if trace is None:
            return False

        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "UPDATE refund_trace SET approval_status='REJECTED', outcome='REJECTED',"
                " approver=?, note=?, updated_at=?"
                " WHERE trace_id=? AND approval_status='PENDING'",
                (approver, note_text, ts, approval_id),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                return False

            conn.execute(
                "INSERT INTO refund_lock (order_id, trace_id, locked_at, reason)"
                " VALUES (?,?,?,?)",
                (trace["order_id"], approval_id, ts, note_text),
            )
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise


def unlock_refund(order_id: str, operator: str, note: str = "") -> dict[str, Any]:
    """运营人工解锁：解除某订单的退款驳回锁，允许用户重新申请。

    只解锁最新一条仍在生效的锁记录，历史留痕不改写。
    """
    order_id = (order_id or "").strip().upper()
    if not order_id:
        raise BizError(E_INVALID, "缺少订单号", "请提供需要解锁的订单号")

    ts = now()
    with connect() as conn:
        row = conn.execute(
            "SELECT id, trace_id, locked_at, reason FROM refund_lock"
            " WHERE order_id=? AND unlocked_at IS NULL"
            " ORDER BY locked_at DESC, id DESC LIMIT 1",
            (order_id,),
        ).fetchone()
        if row is None:
            order = conn.execute(
                "SELECT order_id, status FROM orders WHERE order_id=?", (order_id,)
            ).fetchone()
            if order is None:
                raise BizError(E_NOT_FOUND, f"未找到订单 {order_id}", "")
            return {
                "ok": True,
                "changed": False,
                "message": f"订单 {order_id} 当前没有生效中的退款锁，无需解锁",
            }

        conn.execute(
            "UPDATE refund_lock SET unlocked_at=?, unlocked_by=?, unlock_note=?"
            " WHERE id=?",
            (ts, operator, note or "运营人工解锁", row["id"]),
        )
        order = conn.execute(
            "SELECT * FROM orders WHERE order_id=?", (order_id,)
        ).fetchone()

    return {
        "ok": True,
        "changed": True,
        "order_id": order_id,
        "unlocked_at": ts,
        "unlocked_by": operator,
        "previous_reason": row["reason"],
        "previous_trace": row["trace_id"],
        "order": _shape(order, locked=False),
        "message": f"订单 {order_id} 已解锁，用户可重新申请退款",
    }


def list_locks(order_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """查退款锁记录，含已解锁的历史。"""
    sql = "SELECT * FROM refund_lock WHERE 1=1"
    args: list[Any] = []
    if order_id:
        sql += " AND order_id=?"
        args.append(order_id.strip().upper())
    sql += " ORDER BY locked_at DESC, id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 20), 100)))

    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()

    return [
        {
            "order_id": r["order_id"],
            "trace_id": r["trace_id"],
            "locked_at": r["locked_at"],
            "reason": r["reason"],
            "active": r["unlocked_at"] is None,
            "unlocked_at": r["unlocked_at"],
            "unlocked_by": r["unlocked_by"],
            "unlock_note": r["unlock_note"],
        }
        for r in rows
    ]


def list_refund_traces(
    order_id: str | None = None,
    caller_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """查退款追踪链路。"""
    sql = "SELECT * FROM refund_trace WHERE 1=1"
    args: list[Any] = []
    if order_id:
        sql += " AND order_id=?"
        args.append(order_id.strip().upper())
    if caller_id:
        sql += " AND caller_id=?"
        args.append(caller_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, min(int(limit or 20), 100)))

    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()

    return [
        {
            "trace_id": r["trace_id"],
            "order_id": r["order_id"],
            "caller_id": r["caller_id"],
            "amount": yuan(r["amount_cents"]),
            "reason": r["reason"],
            "shipping_state": r["shipping_state"],
            "approval_mode": r["approval_mode"],
            "approval_status": r["approval_status"],
            "approver": r["approver"],
            "outcome": r["outcome"],
            "note": r["note"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def _new_trace_id() -> str:
    return f"tr_{datetime.now():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}"
