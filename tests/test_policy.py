"""规则引擎、售后分析、审计的测试。

重点验证三件容易被"模型自由发挥"破坏的事：
1. 退款金额由服务端算，模型传超限的金额也只会被拒；
2. 发过货的订单一律转人工，风险分了也不能自动放款；
3. 所有判定与人工操作都留下可追溯的记录。
"""

from __future__ import annotations

import pytest

from mcp_server import analytics, audit, rules, service

UID = "u_1001"          # 张伟
UID2 = "u_1002"         # 李娜
UID3 = "u_1003"         # 王强
MERCHANT = "m_2001"
PWD = "123456"

CANCELLED_UNSHIPPED = "ORD20261015002"   # 已取消未发货，可退 699
CANCELLED_SHIPPED = "ORD20261008009"     # 已取消已发货，走人工
DELIVERED = "ORD20261102005"             # 已签收 458
PARTIAL = "ORD20261103006"               # 李娜，已发货 2999
FULLY_REFUNDED = "ORD20260920003"        # 已全额退款


# ---------------------------------------------------------------- 规则引擎
def test_unshipped_is_auto_approved(db):
    res = service.submit_refund(
        UID, CANCELLED_UNSHIPPED, reason="不想要了", issue_type="NO_LONGER_NEEDED"
    )
    assert res["result"] == "REFUNDED"
    assert res["decision"]["channel"] == "AUTO_APPROVE"
    assert res["decision"]["risk_label"] == "低风险"


def test_shipped_always_manual_even_if_score_low(db):
    """发过货就别想自动放款——这条是硬规则，风险分再低也走人工。"""
    res = service.submit_refund(
        UID, CANCELLED_SHIPPED, reason="拍错了", issue_type="WRONG_ITEM"
    )
    assert res["result"] == "PENDING"
    assert res["decision"]["channel"] == "MANUAL_REVIEW"
    assert res["queue_position"] >= 1


def test_model_cannot_inflate_amount(db):
    """模型/用户报了个大数，只能被拒，不会按这个数退。"""
    with pytest.raises(service.BizError) as exc:
        service.submit_refund(
            UID, CANCELLED_UNSHIPPED, reason="多退点", issue_type="OTHER",
            requested_amount=99999,
        )
    assert exc.value.code == service.E_AMOUNT
    assert "refundable_balance" in exc.value.extra

    # 订单没被改动
    order = service.query_orders(UID, order_id=CANCELLED_UNSHIPPED)["orders"][0]
    assert order["refundable_balance_cents"] == 69900


def test_partial_claim_sets_amount(db):
    res = service.submit_refund(
        UID, CANCELLED_UNSHIPPED, reason="少退一点", issue_type="OTHER",
        requested_amount=100,
    )
    assert res["result"] == "REFUNDED"
    assert res["amount"] == "¥100.00"
    order = service.query_orders(UID, order_id=CANCELLED_UNSHIPPED)["orders"][0]
    assert order["refunded_cents"] == 10000
    assert order["refundable_balance_cents"] == 59900


def test_fully_refunded_is_rejected(db):
    with pytest.raises(service.BizError) as exc:
        service.submit_refund(UID, FULLY_REFUNDED, reason="再退", issue_type="OTHER")
    assert exc.value.code == service.E_ALREADY_REFUNDED


def test_risk_scoring_flags_high_value_delivered(db):
    """高价值 + 已签收 + 全额退，应该被判成高风险。"""
    order = service.query_orders(UID, order_id=DELIVERED)["orders"][0]
    raw = {
        "order_id": order["order_id"], "amount_cents": order["amount_cents"],
        "refunded_cents": 0, "shipped_at": order["shipped_at"],
        "delivered_at": order["delivered_at"],
    }
    d = rules.evaluate(raw, issue_type="QUALITY", user_refunds=[{}] * 3)
    assert d["risk_level"] in ("MEDIUM", "HIGH")
    assert d["decision"] == rules.DECISION_MANUAL
    assert any(r["id"] == "R23" for r in d["rules"])


def test_evaluate_is_deterministic(db):
    order = service.query_orders(UID, order_id=DELIVERED)["orders"][0]
    raw = {
        "order_id": order["order_id"], "amount_cents": order["amount_cents"],
        "refunded_cents": 0, "shipped_at": order["shipped_at"],
        "delivered_at": order["delivered_at"],
    }
    a = rules.evaluate(raw, issue_type="QUALITY", user_refunds=[])
    b = rules.evaluate(raw, issue_type="QUALITY", user_refunds=[])
    assert a["risk_score"] == b["risk_score"]
    assert a["amount_cents"] == b["amount_cents"]


# ---------------------------------------------------------------- 售后查询
def test_query_after_sales_shape(db):
    res = service.query_after_sales(UID)
    assert res["ok"] is True
    assert res["stats"]["total"] >= 2
    assert "records" in res
    assert res["profile"]["order_count"] == 7
    # 只能看到自己的
    assert all(r["refund_id"].startswith("RF") for r in res["records"])


def test_query_after_sales_rejects_others_order(db):
    with pytest.raises(service.BizError) as exc:
        service.query_after_sales(UID, order_id="ORD20261010007")
    assert exc.value.code == service.E_NOT_FOUND


def test_query_after_sales_shows_pending(db):
    res = service.query_after_sales(UID2, order_id="ORD20261010007")
    assert res["stats"]["pending"] == 1
    assert res["records"][0]["status"] == "PENDING"
    assert res["records"][0]["decision_label"]


# ---------------------------------------------------------------- 审核详情
def test_review_detail_contains_basis(db):
    pending = service.pending_queue()[0]
    d = service.review_detail(pending["refund_id"])
    assert d["decision"]["rules"]
    assert d["decision"]["risk_level"]
    assert d["order"]["order_id"] == pending["order_id"]
    assert d["order_history"]
    assert d["user"]["refund_count"] >= 1


def test_approve_writes_review_history(db):
    pending = service.pending_queue()[0]
    service.approve_refund(pending["refund_id"], MERCHANT, note="人工核验通过")
    d = service.review_detail(pending["refund_id"])
    assert d["refund"]["status"] == "APPROVED"
    assert d["refund"]["review_history"][-1]["note"] == "人工核验通过"


def test_human_action_is_audited(db):
    before = audit.stats()["human_actions"]
    pending = service.pending_queue()[0]
    service.reject_refund(pending["refund_id"], MERCHANT, note="凭证不足")
    assert audit.stats()["human_actions"] == before + 1
    last = audit.list_events(kind=audit.KIND_HUMAN_ACTION)[0]
    assert last["payload"]["action"] == "REJECT"
    assert last["payload"]["note"] == "凭证不足"


# ---------------------------------------------------------------- 分析
def test_analytics_totals_match_orders(db):
    data = analytics.summary("ALL")
    t = data["totals"]
    refunds = service.list_refunds(limit=200)
    approved = [r for r in refunds if r["status"] == "APPROVED"]
    assert t["refund_count"] == len(approved)
    assert t["refund_amount_cents"] == sum(r["amount_cents"] for r in approved)
    assert 0 <= t["refund_rate"] <= 100


def test_product_ranking_sorted(db):
    data = analytics.summary("ALL")
    amounts = [p["amount_cents"] for p in data["by_product"]]
    assert amounts == sorted(amounts, reverse=True)
    assert data["by_product"][0]["rank"] == 1


def test_anomaly_multi_refund_detected(db):
    data = analytics.summary("ALL")
    types = {a["type"] for a in data["anomalies"]}
    assert "MULTI_REFUND" in types


def test_anomaly_full_after_delivery_detected(db):
    data = analytics.summary("ALL")
    types = {a["type"] for a in data["anomalies"]}
    assert "FULL_AFTER_DELIVERY" in types


def test_period_filter_narrows(db):
    week = analytics.summary("WEEK")["totals"]
    every = analytics.summary("ALL")["totals"]
    assert week["refund_count"] <= every["refund_count"]


def test_sales_analytics_dimension(db):
    res = service.sales_analytics("ALL", "PRODUCT")
    assert "by_product" in res
    assert "by_issue" not in res
    assert res["text"]

    with pytest.raises(service.BizError):
        service.sales_analytics("ALL", "NOPE")


# ---------------------------------------------------------------- 审计
def test_audit_append_only_seq(db):
    a = audit.log(audit.KIND_USER_MESSAGE, {"text": "第一句"})
    b = audit.log(audit.KIND_AGENT_REPLY, {"text": "第二句"})
    assert b["seq"] == a["seq"] + 1


def test_audit_rejects_unknown_kind(db):
    with pytest.raises(ValueError):
        audit.log("NOT_A_KIND", {})


def test_audit_session_roundtrip(db):
    sid = "test-session-1"
    audit.ensure_session(sid, UID, "张伟", audit.CHANNEL_WEB)
    audit.append_message(sid, "user", "我最近有哪些订单")
    audit.append_message(sid, "assistant", "你有 7 笔订单", [{"type": "reply", "text": "x"}])
    s = audit.get_session(sid)
    assert len(s["messages"]) == 2
    assert s["title"] == "我最近有哪些订单"
    assert audit.list_sessions()[0]["turns"] == 1


def test_rule_decision_is_audited(db):
    service.submit_refund(
        UID, CANCELLED_UNSHIPPED, reason="不想要了", issue_type="NO_LONGER_NEEDED"
    )
    rows = audit.list_events(kind=audit.KIND_RULE_DECISION)
    assert rows
    assert rows[0]["payload"]["amount_cents"] == 69900
    assert rows[0]["payload"]["rules"]
