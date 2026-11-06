"""业务规则矩阵测试。

覆盖三条核心约束：
  1. 只能操作自己的订单
  2. 只能取消一次
  3. 只能退款已取消的订单，且只退一次
"""

from __future__ import annotations

import sqlite3

import pytest

from data.seed import seed
from mcp_server import service

UID = "u_1001"          # 张伟，主测试账号
OTHER = "u_1002"        # 李娜

PAID_ORDER = "ORD20261028001"       # 已支付，未发货，可取消
CREATED_ORDER = "ORD20261101004"    # 待支付，未发货，可取消
CANCELLED_ORDER = "ORD20261015002"  # 已取消、未发货，可直接退款
SHIPPED_CANCELLED = "ORD20261008009"  # 已取消、已发货，退款需人工审批
REFUNDED_ORDER = "ORD20260920003"   # 已退款，终态
COMPLETED_ORDER = "ORD20261102005"  # 已完成，不可取消
OTHERS_ORDER = "ORD20261103006"     # 李娜的已支付订单


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = seed(tmp_path / "test.db")
    monkeypatch.setenv("ORDER_DB_PATH", str(path))
    return path


def status_of(order_id: str) -> str:
    with service.connect() as conn:
        row = conn.execute("SELECT status FROM orders WHERE order_id=?", (order_id,)).fetchone()
    return row["status"]


def log_count(order_id: str, action: str) -> int:
    with service.connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM operation_log WHERE order_id=? AND action=?",
            (order_id, action),
        ).fetchone()[0]


# ---------------------------------------------------------------- 查询
def test_query_lists_only_own_orders(db):
    result = service.query_orders(UID)
    assert result["ok"] is True
    assert result["count"] == 6
    ids = {o["order_id"] for o in result["orders"]}
    assert OTHERS_ORDER not in ids
    assert all(o["status"] in service.VALID_STATUS for o in result["orders"])


def test_query_reports_shipping_state(db):
    shipped = service.query_orders(UID, order_id=SHIPPED_CANCELLED)["orders"][0]
    assert shipped["shipping_state"] == "SHIPPED"
    assert shipped["shipped"] is True
    assert shipped["refund_needs_approval"] is True

    unshipped = service.query_orders(UID, order_id=CANCELLED_ORDER)["orders"][0]
    assert unshipped["shipping_state"] == "NOT_SHIPPED"
    assert unshipped["refund_needs_approval"] is False


def test_query_single_order_ok(db):
    result = service.query_orders(UID, order_id=PAID_ORDER)
    assert result["count"] == 1
    order = result["orders"][0]
    assert order["item_name"].endswith("降噪耳机")
    assert order["amount"] == "¥2,499.00"
    assert order["can_cancel"] is True
    assert order["can_refund"] is False


def test_query_is_case_insensitive(db):
    result = service.query_orders(UID, order_id=PAID_ORDER.lower())
    assert result["count"] == 1


def test_query_others_order_looks_missing(db):
    with pytest.raises(service.BizError) as exc:
        service.query_orders(UID, order_id=OTHERS_ORDER)
    assert exc.value.code == service.E_NOT_FOUND


def test_query_filter_by_status(db):
    result = service.query_orders(UID, status="CANCELLED")
    assert result["count"] >= 1
    assert all(o["status"] == "CANCELLED" for o in result["orders"])


def test_query_invalid_status_rejected(db):
    with pytest.raises(service.BizError) as exc:
        service.query_orders(UID, status="SHIPPED")
    assert exc.value.code == service.E_INVALID


def test_query_limit_is_clamped(db):
    result = service.query_orders(UID, limit=99)
    assert result["count"] <= 20


# ---------------------------------------------------------------- 取消
def test_cancel_paid_order(db):
    result = service.cancel_order(UID, PAID_ORDER, reason="NO_LONGER_NEEDED")
    assert result["ok"] is True
    assert status_of(PAID_ORDER) == "CANCELLED"
    assert result["order"]["can_refund"] is True
    assert log_count(PAID_ORDER, "cancel") == 1


def test_cancel_created_order(db):
    service.cancel_order(UID, CREATED_ORDER)
    assert status_of(CREATED_ORDER) == "CANCELLED"


def test_cancel_twice_rejected(db):
    service.cancel_order(UID, PAID_ORDER)
    with pytest.raises(service.BizError) as exc:
        service.cancel_order(UID, PAID_ORDER)
    assert exc.value.code == service.E_ALREADY_CANCELLED
    assert log_count(PAID_ORDER, "cancel") == 1


def test_cancel_already_cancelled_order(db):
    with pytest.raises(service.BizError) as exc:
        service.cancel_order(UID, CANCELLED_ORDER)
    assert exc.value.code == service.E_ALREADY_CANCELLED


def test_cancel_completed_order_rejected(db):
    with pytest.raises(service.BizError) as exc:
        service.cancel_order(UID, COMPLETED_ORDER)
    assert exc.value.code == service.E_STATE


def test_cancel_others_order_rejected(db):
    with pytest.raises(service.BizError) as exc:
        service.cancel_order(UID, OTHERS_ORDER)
    assert exc.value.code == service.E_NOT_FOUND
    assert status_of(OTHERS_ORDER) == "PAID"


def test_cancel_without_order_id_rejected(db):
    with pytest.raises(service.BizError) as exc:
        service.cancel_order(UID, "   ")
    assert exc.value.code == service.E_INVALID


# ---------------------------------------------------------------- 退款
def test_refund_cancelled_order(db):
    result = service.refund_order(UID, CANCELLED_ORDER)
    assert result["ok"] is True
    assert status_of(CANCELLED_ORDER) == "REFUNDED"
    assert result["order"]["refund_amount"] == "¥699.00"
    assert log_count(CANCELLED_ORDER, "refund") == 1


def test_refund_requires_cancelled_first(db):
    with pytest.raises(service.BizError) as exc:
        service.refund_order(UID, PAID_ORDER)
    assert exc.value.code == service.E_NOT_CANCELLED
    assert "cancel_order" in exc.value.hint
    assert status_of(PAID_ORDER) == "PAID"


def test_refund_twice_rejected(db):
    service.refund_order(UID, CANCELLED_ORDER)
    with pytest.raises(service.BizError) as exc:
        service.refund_order(UID, CANCELLED_ORDER)
    assert exc.value.code == service.E_ALREADY_REFUNDED
    assert log_count(CANCELLED_ORDER, "refund") == 1


def test_refund_already_refunded_order(db):
    with pytest.raises(service.BizError) as exc:
        service.refund_order(UID, REFUNDED_ORDER)
    assert exc.value.code == service.E_ALREADY_REFUNDED


def test_partial_refund(db):
    result = service.refund_order(UID, CANCELLED_ORDER, amount=200.0)
    assert result["order"]["refund_amount"] == "¥200.00"
    assert status_of(CANCELLED_ORDER) == "REFUNDED"


def test_refund_over_amount_rejected(db):
    with pytest.raises(service.BizError) as exc:
        service.refund_order(UID, CANCELLED_ORDER, amount=9999.0)
    assert exc.value.code == service.E_AMOUNT
    assert status_of(CANCELLED_ORDER) == "CANCELLED"


def test_refund_others_order_rejected(db):
    with pytest.raises(service.BizError) as exc:
        service.refund_order(UID, "ORD20261010007")  # 李娜的已取消订单
    assert exc.value.code == service.E_NOT_FOUND


# ---------------------------------------------------------------- 已发货审批
def test_shipped_refund_requires_approval(db):
    with pytest.raises(service.BizError) as exc:
        service.refund_order(UID, SHIPPED_CANCELLED, reason="商品破损")
    assert exc.value.code == service.E_APPROVAL_REQUIRED
    assert exc.value.extra["approval_id"].startswith("tr_")
    assert exc.value.extra["shipping_state"] == "SHIPPED"
    # 只开了审批单，钱还没退
    assert status_of(SHIPPED_CANCELLED) == "CANCELLED"


def test_shipped_refund_after_approval(db):
    with pytest.raises(service.BizError) as exc:
        service.refund_order(UID, SHIPPED_CANCELLED, reason="商品破损")
    approval_id = exc.value.extra["approval_id"]

    result = service.refund_order(
        UID, SHIPPED_CANCELLED, reason="商品破损", approval_id=approval_id
    )
    assert result["ok"] is True
    assert result["approval"] == "APPROVED"
    assert status_of(SHIPPED_CANCELLED) == "REFUNDED"

    trace = service.list_refund_traces(order_id=SHIPPED_CANCELLED)[0]
    assert trace["approval_mode"] == "MANUAL"
    assert trace["approval_status"] == "APPROVED"
    assert trace["outcome"] == "SUCCESS"
    assert trace["shipping_state"] == "SHIPPED"
    assert trace["reason"] == "商品破损"
    assert trace["approver"]


def test_pending_trace_is_reused(db):
    """重复发起申请不应堆出一堆待审批单。"""
    first, second = None, None
    for _ in range(2):
        with pytest.raises(service.BizError) as exc:
            service.refund_order(UID, SHIPPED_CANCELLED, reason="商品破损")
        if first is None:
            first = exc.value.extra["approval_id"]
        else:
            second = exc.value.extra["approval_id"]
    assert first == second
    assert len(service.list_refund_traces(order_id=SHIPPED_CANCELLED)) == 1


def test_approved_amount_cannot_be_changed(db):
    """审批时锁定金额，二次调用改金额无效，防止绕过审批。"""
    with pytest.raises(service.BizError) as exc:
        service.refund_order(UID, SHIPPED_CANCELLED, reason="商品破损")
    approval_id = exc.value.extra["approval_id"]

    result = service.refund_order(
        UID, SHIPPED_CANCELLED, amount=1.0, approval_id=approval_id
    )
    assert result["ok"] is True
    assert result["order"]["refund_amount"] == "¥1,099.00"


def test_rejected_approval_blocks_refund(db):
    with pytest.raises(service.BizError) as exc:
        service.refund_order(UID, SHIPPED_CANCELLED, reason="商品破损")
    approval_id = exc.value.extra["approval_id"]

    assert service.reject_refund(approval_id, "客服主管-陈明", "未通过") is True
    trace = service.list_refund_traces(order_id=SHIPPED_CANCELLED)[0]
    assert trace["approval_status"] == "REJECTED"
    assert trace["outcome"] == "REJECTED"

    # 驳回后订单进入锁定态，比「审批单无效」更早、更精确地拦下来
    with pytest.raises(service.BizError) as exc2:
        service.refund_order(UID, SHIPPED_CANCELLED, approval_id=approval_id)
    assert exc2.value.code == service.E_REFUND_LOCKED
    assert status_of(SHIPPED_CANCELLED) == "CANCELLED"


def test_fake_approval_id_rejected(db):
    """伪造的审批凭证必须无效。"""
    with pytest.raises(service.BizError) as exc:
        service.refund_order(UID, SHIPPED_CANCELLED, approval_id="tr_fake_20260101")
    assert exc.value.code == service.E_APPROVAL_INVALID
    assert status_of(SHIPPED_CANCELLED) == "CANCELLED"


def test_approval_id_bound_to_its_order(db):
    """审批凭证和订单绑定，不能拿去退另一笔单。"""
    extra = "ORD20261009010"
    with service.connect() as conn:
        conn.execute(
            "INSERT INTO orders (order_id, owner_id, owner_name, item_name, amount_cents,"
            " status, created_at, paid_at, shipped_at, cancelled_at)"
            " VALUES (?,?,?,?,?,'CANCELLED',?,?,?,?)",
            (extra, UID, "张伟", "测试用已发货耳机", 88000,
             "2026-10-09 10:00:00", "2026-10-09 10:01:00",
             "2026-10-09 15:00:00", "2026-10-10 09:00:00"),
        )

    with pytest.raises(service.BizError) as exc:
        service.refund_order(UID, SHIPPED_CANCELLED, reason="测试")
    approval_id = exc.value.extra["approval_id"]

    with pytest.raises(service.BizError) as exc2:
        service.refund_order(UID, extra, approval_id=approval_id)
    assert exc2.value.code == service.E_APPROVAL_INVALID
    assert status_of(extra) == "CANCELLED"


def test_unshipped_refund_trace_is_auto(db):
    service.refund_order(UID, CANCELLED_ORDER, reason="不想要了")
    trace = service.list_refund_traces(order_id=CANCELLED_ORDER)[0]
    assert trace["approval_mode"] == "AUTO"
    assert trace["approval_status"] == "NOT_REQUIRED"
    assert trace["shipping_state"] == "NOT_SHIPPED"
    assert trace["reason"] == "不想要了"
    assert trace["approver"] is None


def test_refund_reason_defaulted_when_missing(db):
    service.refund_order(UID, CANCELLED_ORDER)
    trace = service.list_refund_traces(order_id=CANCELLED_ORDER)[0]
    assert trace["reason"] == "未填写退款原因"


# ---------------------------------------------------------------- 端到端
def test_full_lifecycle_paid_to_refunded(db):
    service.cancel_order(UID, PAID_ORDER, reason="PRICE_ISSUE")
    assert status_of(PAID_ORDER) == "CANCELLED"

    result = service.refund_order(UID, PAID_ORDER, reason="价格保护")
    assert result["ok"] is True
    assert status_of(PAID_ORDER) == "REFUNDED"
    assert result["order"]["can_cancel"] is False
    assert result["order"]["can_refund"] is False


def test_operation_log_unique_constraint(db):
    """即使应用层被绕过，数据库也不允许同一订单重复写同一种操作。"""
    service.cancel_order(UID, PAID_ORDER)
    with service.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO operation_log (order_id, action, actor_id, detail, created_at)"
                " VALUES (?,?,?,?,?)",
                (PAID_ORDER, "cancel", UID, "重复写入", service.now()),
            )


def test_unknown_caller_rejected(db):
    with pytest.raises(service.BizError) as exc:
        service.resolve_caller("u_9999")
    assert exc.value.code == service.E_UNKNOWN_USER


# ---------------------------------------------------------------- 驳回锁定
def _approve_flow(order_id: str) -> str:
    """走一遍已发货的退款审批开单，返回 approval_id。"""
    with pytest.raises(service.BizError) as exc:
        service.refund_order(UID, order_id, reason="测试驳回")
    assert exc.value.code == service.E_APPROVAL_REQUIRED
    return exc.value.extra["approval_id"]


def test_reject_locks_order_then_refund_blocked(db):
    """驳回后退款申请被锁，用户不能自行再申请。"""
    approval_id = _approve_flow(SHIPPED_CANCELLED)
    assert service.reject_refund(approval_id, approver="客服主管", note="凭证不足") is True

    with pytest.raises(service.BizError) as exc:
        service.refund_order(UID, SHIPPED_CANCELLED, reason="再试一次")
    assert exc.value.code == service.E_REFUND_LOCKED
    assert status_of(SHIPPED_CANCELLED) == "CANCELLED"

    # 换理由、换金额都绕不过去
    with pytest.raises(service.BizError) as exc2:
        service.refund_order(UID, SHIPPED_CANCELLED, amount=1.0, reason="换个说法")
    assert exc2.value.code == service.E_REFUND_LOCKED


def test_locked_flag_visible_in_query(db):
    approval_id = _approve_flow(SHIPPED_CANCELLED)
    service.reject_refund(approval_id, approver="客服主管")

    info = service.query_orders(UID, order_id=SHIPPED_CANCELLED)["orders"][0]
    assert info["refund_locked"] is True
    assert info["can_refund"] is True  # 状态上仍可退，只是被锁拦住

    other = service.query_orders(UID, order_id=CANCELLED_ORDER)["orders"][0]
    assert other["refund_locked"] is False


def test_unlock_restores_refund_ability(db):
    """人工解锁后可重新走审批，且历史锁记录保留。"""
    approval_id = _approve_flow(SHIPPED_CANCELLED)
    service.reject_refund(approval_id, approver="客服主管")

    result = service.unlock_refund(SHIPPED_CANCELLED, operator="运营-陈明", note="复核通过")
    assert result["changed"] is True
    assert result["previous_trace"] == approval_id

    locks = service.list_locks(SHIPPED_CANCELLED)
    assert len(locks) == 1
    assert locks[0]["active"] is False
    assert locks[0]["unlocked_by"] == "运营-陈明"

    # 解锁后重新开审批单，并能正常放款
    new_approval = _approve_flow(SHIPPED_CANCELLED)
    assert new_approval != approval_id
    assert service.refund_order(UID, SHIPPED_CANCELLED, approval_id=new_approval)["ok"] is True
    assert status_of(SHIPPED_CANCELLED) == "REFUNDED"


def test_unlock_without_lock_is_noop(db):
    result = service.unlock_refund(CANCELLED_ORDER, operator="运营")
    assert result["changed"] is False
    assert "没有生效中的退款锁" in result["message"]


def test_unlock_unknown_order_rejected(db):
    with pytest.raises(service.BizError) as exc:
        service.unlock_refund("ORD_NOT_EXIST_0001", operator="运营")
    assert exc.value.code == service.E_NOT_FOUND


def test_approve_after_reject_normal_path_still_blocked(db):
    """驳回后即使拿到旧 approval_id 也不能放款。"""
    approval_id = _approve_flow(SHIPPED_CANCELLED)
    service.reject_refund(approval_id, approver="客服主管")

    with pytest.raises(service.BizError) as exc:
        service.refund_order(UID, SHIPPED_CANCELLED, approval_id=approval_id)
    assert exc.value.code == service.E_REFUND_LOCKED
    assert status_of(SHIPPED_CANCELLED) == "CANCELLED"
