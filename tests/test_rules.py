"""业务规则矩阵测试。

覆盖六条核心约束：
  1. 账号密码认证与角色隔离
  2. 只能操作自己的订单
  3. 只能取消一次，终态不可逆
  4. 未发货直接退，已发货/已签收走申请
  5. 退款金额累加，累计不超过实付
  6. 驳回锁定与人工解锁
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from mcp_server import service, store

UID = "u_1001"          # 张伟
UID2 = "u_1002"         # 李娜
MERCHANT = "m_2001"     # 陈明，商家
CUSTOMER_PWD = "123456"

PAID = "ORD20261028001"                 # 已支付，未发货，可取消
CANCELLED_UNSHIPPED = "ORD20261015002"  # 已取消，未发货 -> 直接退
CANCELLED_SHIPPED = "ORD20261008009"    # 已取消，已发货 -> 申请审批
REFUNDED = "ORD20260920003"             # 已全额退款，终态
COMPLETED = "ORD20261102005"            # 已完成，已签收 -> 可申请退款
OTHERS = "ORD20261103006"               # 李娜的订单（越权用）
PARTIAL = "ORD20261010007"              # 李娜的，已退 ¥1,000，预置一条 PENDING


# ---------------------------------------------------------------- 工具
def order_of(order_id: str) -> dict:
    orders = store.read_only(store.ORDERS_FILE, store.empty_orders())["orders"]
    return orders[order_id]


def refund_cents(order_id: str) -> int:
    return order_of(order_id).get("refunded_cents", 0)


def pending_of(order_id: str) -> list[dict]:
    data = store.read_only(store.REFUNDS_FILE, store.empty_refunds())
    return [
        r for r in data["refunds"].values()
        if r["order_id"] == order_id and r["status"] == "PENDING"
    ]


def drop_pending(order_id: str) -> None:
    """清掉种子数据里的待审批申请，让测试从干净状态开始。"""
    with store.transaction(store.REFUNDS_FILE, store.empty_refunds()) as data:
        for rid in [
            r["refund_id"] for r in data["refunds"].values()
            if r["order_id"] == order_id and r["status"] == "PENDING"
        ]:
            del data["refunds"][rid]


def submit(order_id: str, amount: float | None = None, user: str = UID2) -> dict:
    return service.refund_order(user, order_id, amount=amount, reason="测试")


# ---------------------------------------------------------------- 认证
def test_login_success(db):
    user = service.authenticate("zhangwei", CUSTOMER_PWD)
    assert user["user_id"] == UID
    assert user["role"] == "customer"
    assert "password_hash" not in user


def test_login_username_case_insensitive(db):
    assert service.authenticate("ZHANGWEI", CUSTOMER_PWD)["user_id"] == UID


def test_login_wrong_password(db):
    with pytest.raises(service.BizError) as exc:
        service.authenticate("zhangwei", "wrong-pass")
    assert exc.value.code == service.E_AUTH_FAILED


def test_login_unknown_user(db):
    with pytest.raises(service.BizError) as exc:
        service.authenticate("nobody", CUSTOMER_PWD)
    assert exc.value.code == service.E_AUTH_FAILED


def test_customer_cannot_login_as_merchant(db):
    with pytest.raises(service.BizError) as exc:
        service.authenticate("zhangwei", CUSTOMER_PWD, role="merchant")
    assert exc.value.code == service.E_FORBIDDEN


def test_password_is_hashed(db):
    users = store.read_only(store.USERS_FILE, store.empty_users())["users"]
    for u in users.values():
        assert u["password_hash"].startswith("pbkdf2_sha256$")
        assert CUSTOMER_PWD not in u["password_hash"]


# ---------------------------------------------------------------- 查询
def test_query_lists_only_own_orders(db):
    result = service.query_orders(UID)
    assert result["ok"] is True
    ids = {o["order_id"] for o in result["orders"]}
    assert OTHERS not in ids
    assert len(ids) == 6


def test_query_others_order_reports_not_found(db):
    with pytest.raises(service.BizError) as exc:
        service.query_orders(UID, order_id=OTHERS)
    assert exc.value.code == service.E_NOT_FOUND


def test_query_exposes_refund_balance(db):
    o = service.query_orders(UID2, order_id=PARTIAL)["orders"][0]
    assert o["amount_cents"] == 479900
    assert o["refunded_cents"] == 100000
    assert o["refundable_balance_cents"] == 379900
    assert o["pending_refund"] is not None


# ---------------------------------------------------------------- 取消
def test_cancel_once(db):
    res = service.cancel_order(UID, PAID, reason="NO_LONGER_NEEDED")
    assert res["ok"] is True
    assert order_of(PAID)["status"] == "CANCELLED"

    with pytest.raises(service.BizError) as exc:
        service.cancel_order(UID, PAID)
    assert exc.value.code == service.E_ALREADY_CANCELLED


def test_cancel_completed_rejected(db):
    with pytest.raises(service.BizError) as exc:
        service.cancel_order(UID, COMPLETED)
    assert exc.value.code == service.E_STATE


def test_cancel_others_order(db):
    with pytest.raises(service.BizError) as exc:
        service.cancel_order(UID, OTHERS)
    assert exc.value.code == service.E_NOT_FOUND
    assert order_of(OTHERS)["status"] == "PAID"


# ---------------------------------------------------------------- 直接退款（未发货）
def test_unshipped_refund_is_immediate(db):
    res = service.refund_order(UID, CANCELLED_UNSHIPPED, reason="拍错了")
    assert res["result"] == "REFUNDED"
    assert order_of(CANCELLED_UNSHIPPED)["status"] == "REFUNDED"
    assert refund_cents(CANCELLED_UNSHIPPED) == 69900


def test_unshipped_partial_refund_then_rest(db):
    """分次退款：先退 300，再退剩余 399，累计等于实付。"""
    first = service.refund_order(UID, CANCELLED_UNSHIPPED, amount=300)
    assert first["result"] == "REFUNDED"
    assert refund_cents(CANCELLED_UNSHIPPED) == 30000
    assert order_of(CANCELLED_UNSHIPPED)["status"] == "CANCELLED"

    second = service.refund_order(UID, CANCELLED_UNSHIPPED)
    assert second["result"] == "REFUNDED"
    assert refund_cents(CANCELLED_UNSHIPPED) == 69900
    assert order_of(CANCELLED_UNSHIPPED)["status"] == "REFUNDED"


def test_refund_fully_refunded_order(db):
    with pytest.raises(service.BizError) as exc:
        service.refund_order(UID, REFUNDED)
    assert exc.value.code == service.E_ALREADY_REFUNDED


def test_paid_order_cannot_refund_directly(db):
    with pytest.raises(service.BizError) as exc:
        service.refund_order(UID, PAID, reason="想退")
    assert exc.value.code == service.E_NOT_REFUNDABLE


# ---------------------------------------------------------------- 申请退款（已发货/已签收）
def test_shipped_refund_creates_pending(db):
    res = service.refund_order(UID, CANCELLED_SHIPPED, amount=500, reason="有杂音")
    assert res["result"] == "PENDING"
    assert res["queue_position"] >= 1
    assert order_of(CANCELLED_SHIPPED)["status"] == "CANCELLED"
    assert refund_cents(CANCELLED_SHIPPED) == 0  # 申请阶段不动钱
    assert len(pending_of(CANCELLED_SHIPPED)) == 1


def test_completed_order_can_apply(db):
    """已签收订单走申请，不再被终态拦截。"""
    res = service.refund_order(UID, COMPLETED, reason="键帽松动")
    assert res["result"] == "PENDING"
    assert order_of(COMPLETED)["status"] == "COMPLETED"


def test_duplicate_application_blocked(db):
    service.refund_order(UID, CANCELLED_SHIPPED, amount=500)
    with pytest.raises(service.BizError) as exc:
        service.refund_order(UID, CANCELLED_SHIPPED, amount=100)
    assert exc.value.code == service.E_PENDING_EXISTS


def test_refund_others_order(db):
    with pytest.raises(service.BizError) as exc:
        service.refund_order(UID, OTHERS, reason="越权")
    assert exc.value.code == service.E_NOT_FOUND


# ---------------------------------------------------------------- 累计金额
def test_amount_exceeds_balance(db):
    drop_pending(PARTIAL)
    with pytest.raises(service.BizError) as exc:
        submit(PARTIAL, amount=4000, user=UID2)
    assert exc.value.code == service.E_AMOUNT
    assert exc.value.extra["refundable_balance"] == "¥3,799.00"


def test_cumulative_across_two_approvals(db):
    """已退 1000，再退 500 批，再退 1000 批，累计 2500。"""
    drop_pending(PARTIAL)

    r1 = submit(PARTIAL, amount=500)
    service.approve_refund(r1["refund_id"], MERCHANT)
    assert refund_cents(PARTIAL) == 150000

    r2 = submit(PARTIAL, amount=1000)
    service.approve_refund(r2["refund_id"], MERCHANT)
    assert refund_cents(PARTIAL) == 250000
    assert order_of(PARTIAL)["amount_cents"] - refund_cents(PARTIAL) == 229900


def test_cumulative_never_exceeds_after_approvals(db):
    drop_pending(PARTIAL)
    r1 = submit(PARTIAL, amount=3799)
    service.approve_refund(r1["refund_id"], MERCHANT)
    assert order_of(PARTIAL)["status"] == "REFUNDED"

    with pytest.raises(service.BizError) as exc:
        submit(PARTIAL, amount=1, user=UID2)
    assert exc.value.code == service.E_ALREADY_REFUNDED


def test_partial_then_full_marks_refunded(db):
    drop_pending(PARTIAL)
    r1 = submit(PARTIAL, amount=1000)
    service.approve_refund(r1["refund_id"], MERCHANT)
    assert order_of(PARTIAL)["status"] == "COMPLETED"

    r2 = submit(PARTIAL)  # 省略金额 = 退剩余全部
    service.approve_refund(r2["refund_id"], MERCHANT)
    assert refund_cents(PARTIAL) == 479900
    assert order_of(PARTIAL)["status"] == "REFUNDED"


# ---------------------------------------------------------------- 审批权限与幂等
def test_customer_cannot_approve(db):
    res = service.refund_order(UID, CANCELLED_SHIPPED, amount=500)
    with pytest.raises(service.BizError) as exc:
        service.approve_refund(res["refund_id"], UID)
    assert exc.value.code == service.E_FORBIDDEN


def test_approve_twice_blocked(db):
    res = service.refund_order(UID, CANCELLED_SHIPPED, amount=500)
    service.approve_refund(res["refund_id"], MERCHANT)

    with pytest.raises(service.BizError) as exc:
        service.approve_refund(res["refund_id"], MERCHANT)
    assert exc.value.code == service.E_REFUND_HANDLED
    assert refund_cents(CANCELLED_SHIPPED) == 50000


def test_rejected_application_cannot_be_approved(db):
    res = service.refund_order(UID, CANCELLED_SHIPPED, amount=500)
    service.reject_refund(res["refund_id"], MERCHANT, note="材料不足")

    with pytest.raises(service.BizError) as exc:
        service.approve_refund(res["refund_id"], MERCHANT)
    assert exc.value.code == service.E_REFUND_HANDLED
    assert refund_cents(CANCELLED_SHIPPED) == 0


def test_unknown_refund_id(db):
    with pytest.raises(service.BizError) as exc:
        service.approve_refund("RF_NOT_EXIST", MERCHANT)
    assert exc.value.code == service.E_REFUND_NOT_FOUND


# ---------------------------------------------------------------- 驳回锁定
def test_reject_locks_order(db):
    res = service.refund_order(UID, CANCELLED_SHIPPED, amount=500)
    service.reject_refund(res["refund_id"], MERCHANT, note="凭证不足")

    assert service.list_locks()[0]["order_id"] == CANCELLED_SHIPPED
    with pytest.raises(service.BizError) as exc:
        service.refund_order(UID, CANCELLED_SHIPPED, amount=100)
    assert exc.value.code == service.E_REFUND_LOCKED


def test_locked_flag_in_query(db):
    res = service.refund_order(UID, CANCELLED_SHIPPED, amount=500)
    service.reject_refund(res["refund_id"], MERCHANT)
    o = service.query_orders(UID, order_id=CANCELLED_SHIPPED)["orders"][0]
    assert o["refund_locked"] is True
    assert o["can_refund"] is False


def test_unlock_allows_reapply(db):
    res = service.refund_order(UID, CANCELLED_SHIPPED, amount=500)
    service.reject_refund(res["refund_id"], MERCHANT)

    out = service.unlock_order(CANCELLED_SHIPPED, MERCHANT, note="复核通过")
    assert out["changed"] is True

    again = service.refund_order(UID, CANCELLED_SHIPPED, amount=500, reason="补充材料")
    assert again["result"] == "PENDING"
    service.approve_refund(again["refund_id"], MERCHANT)
    assert refund_cents(CANCELLED_SHIPPED) == 50000


def test_unlock_without_lock_is_noop(db):
    out = service.unlock_order(COMPLETED, MERCHANT)
    assert out["changed"] is False


def test_unlock_requires_merchant(db):
    with pytest.raises(service.BizError) as exc:
        service.unlock_order(COMPLETED, UID)
    assert exc.value.code == service.E_FORBIDDEN


# ---------------------------------------------------------------- 并发
def test_concurrent_submit_creates_single_pending(db):
    """多个线程同时提交，文件锁应保证只落一条 PENDING。"""
    drop_pending(PARTIAL)

    def attempt(_):
        try:
            service.refund_order(UID2, PARTIAL, amount=100, reason="并发")
            return "ok"
        except service.BizError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))

    assert results.count("ok") == 1
    assert len(pending_of(PARTIAL)) == 1


def test_concurrent_approve_only_once(db):
    drop_pending(PARTIAL)
    res = submit(PARTIAL, amount=1000)

    def attempt(_):
        try:
            service.approve_refund(res["refund_id"], MERCHANT)
            return "ok"
        except service.BizError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))

    assert results.count("ok") == 1
    # 原有 1000 + 本次 1000
    assert refund_cents(PARTIAL) == 200000


# ---------------------------------------------------------------- 队列与统计
def test_pending_queue_is_ordered(db):
    queue = service.pending_queue()
    seqs = [r["seq"] for r in queue]
    assert seqs == sorted(seqs)
    for idx, item in enumerate(queue, start=1):
        assert item["queue_position"] == idx
        assert item["status"] == "PENDING"


def test_dashboard_stats(db):
    stats = service.dashboard_stats()
    assert stats["pending_count"] == 1
    assert stats["order_count"] == 9
