"""生成测试数据：4 个账号、12 笔订单、7 条退款记录，写入 JSON 文件。

用法:
    python -m data.seed          # 重建全部数据文件（含审计与会话）

演示账号：
    zhangwei  / 123456     张伟，顾客
    lina      / 123456     李娜，顾客
    wangqiang / 123456     王强，顾客
    merchant  / admin123   陈明，商家（后台审批用）

数据设计目标：覆盖全部订单状态与全部退款路径，让后台四个页面一打开就有内容。
- 未发货已取消 -> 自动放款（mode=AUTO）
- 已发货/已签收 -> 人工审批（含 APPROVED / PENDING / REJECTED）
- 部分退款与全额退款、二次退款都有样例，便于验证累计金额不超额
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from mcp_server import audit, store  # noqa: E402

# 演示账号密码。生产环境请通过环境变量覆盖。
CUSTOMER_PASSWORD = os.getenv("DEMO_CUSTOMER_PASSWORD", "123456")
MERCHANT_PASSWORD = os.getenv("DEMO_MERCHANT_PASSWORD", "admin123")


def _user(user_id, username, name, role, password):
    return {
        "user_id": user_id,
        "username": username,
        "name": name,
        "role": role,
        "password_hash": store.hash_password(password),
        "created_at": "2026-09-01 09:00:00",
    }


USERS = [
    _user("u_1001", "zhangwei", "张伟", "customer", CUSTOMER_PASSWORD),
    _user("u_1002", "lina", "李娜", "customer", CUSTOMER_PASSWORD),
    _user("u_1003", "wangqiang", "王强", "customer", CUSTOMER_PASSWORD),
    _user("m_2001", "merchant", "陈明", "merchant", MERCHANT_PASSWORD),
]

ORDERS = [
    # ---------- 张伟：覆盖各种组合 ----------
    dict(
        order_id="ORD20261028001", owner_id="u_1001", owner_name="张伟",
        item_name="索尼 WH-1000XM5 头戴式降噪耳机", item_desc="黑色 / 国行 / 质保一年",
        amount_cents=249900, status="PAID",
        created_at="2026-10-28 20:14:06", paid_at="2026-10-28 20:15:33",
        shipped_at=None, delivered_at=None,
        cancelled_at=None, cancel_reason=None,
        refunded_cents=0, refunded_at=None,
        tracking_no=None, updated_at="2026-10-28 20:15:33",
    ),
    dict(
        order_id="ORD20261015002", owner_id="u_1001", owner_name="张伟",
        item_name="罗技 MX Master 3S 无线鼠标", item_desc="石墨黑 / 8000DPI",
        amount_cents=69900, status="CANCELLED",
        created_at="2026-10-15 09:02:11", paid_at="2026-10-15 09:03:40",
        shipped_at=None, delivered_at=None,
        cancelled_at="2026-10-16 11:20:08", cancel_reason="NO_LONGER_NEEDED",
        refunded_cents=0, refunded_at=None,
        tracking_no=None, updated_at="2026-10-16 11:20:08",
    ),
    dict(
        order_id="ORD20261008009", owner_id="u_1001", owner_name="张伟",
        item_name="罗技 G Pro X 2 无线游戏耳机", item_desc="白色 / 7.1 声道",
        amount_cents=109900, status="CANCELLED",
        created_at="2026-10-08 15:40:22", paid_at="2026-10-08 15:41:05",
        shipped_at="2026-10-09 09:30:22", delivered_at=None,
        cancelled_at="2026-10-11 10:05:00", cancel_reason="WRONG_ITEM",
        refunded_cents=0, refunded_at=None,
        tracking_no="JD0098771234", updated_at="2026-10-11 10:05:00",
    ),
    dict(
        order_id="ORD20260920003", owner_id="u_1001", owner_name="张伟",
        item_name="Apple AirPods Pro 2 无线降噪耳机", item_desc="USB-C 版 / 国行",
        amount_cents=189900, status="REFUNDED",
        created_at="2026-09-20 21:03:14", paid_at="2026-09-20 21:04:02",
        shipped_at="2026-09-21 10:12:00", delivered_at="2026-09-23 14:22:10",
        cancelled_at="2026-09-24 09:00:00", cancel_reason="OTHER",
        refunded_cents=189900, refunded_at="2026-09-25 16:30:44",
        tracking_no="SF1234567890", updated_at="2026-09-25 16:30:44",
    ),
    dict(
        order_id="ORD20261101004", owner_id="u_1001", owner_name="张伟",
        item_name="小米移动电源 3 20000mAh", item_desc="白色 / 双向快充",
        amount_cents=12900, status="CREATED",
        created_at="2026-11-01 12:30:00", paid_at=None,
        shipped_at=None, delivered_at=None,
        cancelled_at=None, cancel_reason=None,
        refunded_cents=0, refunded_at=None,
        tracking_no=None, updated_at="2026-11-01 12:30:00",
    ),
    dict(
        order_id="ORD20261102005", owner_id="u_1001", owner_name="张伟",
        item_name="CHERRY MX Board 3.0S 机械键盘", item_desc="红轴 / 108 键 / 白色",
        amount_cents=45800, status="COMPLETED",
        created_at="2026-10-20 19:00:00", paid_at="2026-10-20 19:01:12",
        shipped_at="2026-10-21 09:00:00", delivered_at="2026-10-23 11:45:00",
        cancelled_at=None, cancel_reason=None,
        refunded_cents=0, refunded_at=None,
        tracking_no="YT7788990011", updated_at="2026-10-23 11:45:00",
    ),
    dict(
        order_id="ORD20261016013", owner_id="u_1001", owner_name="张伟",
        item_name="Anker 736 氮化镓充电器 65W", item_desc="白色 / 双 C 口",
        amount_cents=15900, status="REFUNDED",
        created_at="2026-10-16 10:50:00", paid_at="2026-10-16 10:51:00",
        shipped_at=None, delivered_at=None,
        cancelled_at="2026-10-16 11:20:08", cancel_reason="NO_LONGER_NEEDED",
        refunded_cents=15900, refunded_at="2026-10-16 11:21:00",
        tracking_no=None, updated_at="2026-10-16 11:21:00",
    ),
    # ---------- 李娜：越权测试 + 部分退款 + 二次退款 ----------
    dict(
        order_id="ORD20261103006", owner_id="u_1002", owner_name="李娜",
        item_name="戴森 Supersonic HD15 吹风机", item_desc="紫红色 / 礼盒装",
        amount_cents=299900, status="PAID",
        created_at="2026-11-03 10:00:00", paid_at="2026-11-03 10:02:00",
        shipped_at="2026-11-04 08:00:00", delivered_at=None,
        cancelled_at=None, cancel_reason=None,
        refunded_cents=0, refunded_at=None,
        tracking_no="SF9988776655", updated_at="2026-11-04 08:00:00",
    ),
    dict(
        order_id="ORD20261010007", owner_id="u_1002", owner_name="李娜",
        item_name="Apple iPad Air 11 英寸 (M3)", item_desc="128GB / 深空灰 / WiFi",
        amount_cents=479900, status="COMPLETED",
        created_at="2026-10-10 14:20:00", paid_at="2026-10-10 14:21:30",
        shipped_at="2026-10-11 09:00:00", delivered_at="2026-10-13 16:10:00",
        cancelled_at=None, cancel_reason=None,
        refunded_cents=100000, refunded_at="2026-10-28 11:00:00",
        tracking_no="JD5566778899", updated_at="2026-10-28 11:00:00",
    ),
    dict(
        order_id="ORD20260928010", owner_id="u_1002", owner_name="李娜",
        item_name="北鼎养生壶 K159 全玻璃", item_desc="1.5L / 带滤网",
        amount_cents=89900, status="REFUNDED",
        created_at="2026-09-28 08:40:00", paid_at="2026-09-28 08:41:10",
        shipped_at="2026-09-29 10:00:00", delivered_at="2026-10-01 15:20:00",
        cancelled_at=None, cancel_reason=None,
        refunded_cents=89900, refunded_at="2026-10-04 09:30:00",
        tracking_no="ZTO5566778899", updated_at="2026-10-04 09:30:00",
    ),
    # ---------- 王强：越权测试 + 异常订单素材 ----------
    dict(
        order_id="ORD20261104008", owner_id="u_1003", owner_name="王强",
        item_name="德龙 ECAM22.110 全自动咖啡机", item_desc="银色 / 家用",
        amount_cents=268000, status="PAID",
        created_at="2026-11-04 11:15:00", paid_at="2026-11-04 11:16:40",
        shipped_at=None, delivered_at=None,
        cancelled_at=None, cancel_reason=None,
        refunded_cents=0, refunded_at=None,
        tracking_no=None, updated_at="2026-11-04 11:16:40",
    ),
    dict(
        order_id="ORD20261020011", owner_id="u_1003", owner_name="王强",
        item_name="罗技 K380 蓝牙键盘", item_desc="深灰 / 多设备切换",
        amount_cents=19900, status="COMPLETED",
        created_at="2026-10-20 10:00:00", paid_at="2026-10-20 10:01:00",
        shipped_at="2026-10-20 16:00:00", delivered_at="2026-10-22 09:30:00",
        cancelled_at=None, cancel_reason=None,
        refunded_cents=0, refunded_at=None,
        tracking_no="YT1122334455", updated_at="2026-10-22 09:30:00",
    ),
    dict(
        order_id="ORD20261005012", owner_id="u_1003", owner_name="王强",
        item_name="小米电视 S Pro 65 英寸", item_desc="Mini LED / 4K 144Hz",
        amount_cents=329900, status="COMPLETED",
        created_at="2026-10-05 19:10:00", paid_at="2026-10-05 19:12:00",
        shipped_at="2026-10-06 09:00:00", delivered_at="2026-10-08 14:00:00",
        cancelled_at=None, cancel_reason=None,
        refunded_cents=80000, refunded_at="2026-10-25 10:00:00",
        tracking_no="SF2233445566", updated_at="2026-10-25 10:00:00",
    ),
]


def _decision(risk_level, risk_score, priority, rules, amount_reason, summary,
              decision="MANUAL_REVIEW", needs_evidence=False):
    """构造一条历史判定，字段与 rules.evaluate 保持一致，供审核页展示。"""
    label = {"AUTO_APPROVE": "自动放款", "MANUAL_REVIEW": "转人工审核",
             "REJECT": "拒绝"}[decision]
    risk = {"LOW": "低风险", "MEDIUM": "中风险", "HIGH": "高风险"}[risk_level]
    return {
        "decision": decision,
        "decision_label": label,
        "risk_level": risk_level,
        "risk_label": risk,
        "risk_score": risk_score,
        "priority": priority,
        "amount_reason": amount_reason,
        "summary": summary,
        "rules": rules,
        "needs_evidence": needs_evidence,
    }


def _rule(rid, name, delta, detail):
    return {"id": rid, "name": name, "delta": delta, "detail": detail}


REFUNDS = [
    # 未发货已取消 -> 自动放款
    dict(
        refund_id="RF20261016112100B0C1", order_id="ORD20261016013",
        user_id="u_1001", user_name="张伟", amount_cents=15900,
        requested_cents=None, issue_type="NO_LONGER_NEEDED",
        reason="拍错了，取消后直接退", status="APPROVED", mode="AUTO",
        seq=1, created_at="2026-10-16 11:20:40", reviewed_at="2026-10-16 11:20:40",
        reviewer_id="system", reviewer_name="系统自动放款",
        review_note="规则引擎自动放款，无需人工审批",
        decision=_decision(
            "LOW", 0, "P2",
            [_rule("R10", "未发货", -35, "订单未发货，不涉及退货验货，资金风险最低"),
             _rule("R18", "首次售后", -10, "该用户此前没有退款记录")],
            "用户未指定金额，按可退余额全额退",
            "自动放款（低风险，风险分 0）；无风险项命中",
            decision="AUTO_APPROVE",
        ),
        review_history=[],
    ),
    # 已签收后全额退（异常：FULL_AFTER_DELIVERY）
    dict(
        refund_id="RF20260925163044A1B2", order_id="ORD20260920003",
        user_id="u_1001", user_name="张伟", amount_cents=189900,
        requested_cents=None, issue_type="NOT_AS_DESCRIBED",
        reason="降噪效果与页面描述不符，申请退货退款", status="APPROVED", mode="MANUAL",
        seq=2, created_at="2026-09-24 09:05:00", reviewed_at="2026-09-25 16:30:44",
        reviewer_id="m_2001", reviewer_name="陈明", review_note="已核验退货入库",
        decision=_decision(
            "HIGH", 55, "P0",
            [_rule("R12", "已签收", 20, "已签收，需核验退货入库或凭证"),
             _rule("R13", "高价值订单", 20, "实付 ¥1,899.00，达高价值阈值"),
             _rule("R21", "需凭证类问题", 5, "与描述不符，人工需核验图片或检测报告"),
             _rule("R23", "已签收后全额退", 15, "已签收且退全部余额，需确认退货实物")],
            "用户未指定金额，按可退余额全额退",
            "转人工审核（高风险，风险分 55）；主因：已签收、高价值订单",
            needs_evidence=True,
        ),
        review_history=[{
            "action": "APPROVE", "action_label": "审批通过", "at": "2026-09-25 16:30:44",
            "by_id": "m_2001", "by": "陈明", "note": "已核验退货入库",
            "amount_cents": 189900,
        }],
    ),
    # 部分退款
    dict(
        refund_id="RF20261028110000C3D4", order_id="ORD20261010007",
        user_id="u_1002", user_name="李娜", amount_cents=100000,
        requested_cents=100000, issue_type="NOT_AS_DESCRIBED",
        reason="屏幕有细微划痕，协商部分补偿", status="APPROVED", mode="MANUAL",
        seq=3, created_at="2026-10-27 20:00:00", reviewed_at="2026-10-28 11:00:00",
        reviewer_id="m_2001", reviewer_name="陈明", review_note="协商退 1000 元",
        decision=_decision(
            "MEDIUM", 45, "P1",
            [_rule("R12", "已签收", 20, "已签收，需核验退货入库或凭证"),
             _rule("R13", "高价值订单", 20, "实付 ¥4,799.00，达高价值阈值"),
             _rule("R21", "需凭证类问题", 5, "与描述不符，人工需核验图片或检测报告")],
            "用户主张部分退款，按主张金额执行，剩余 ¥3,799.00 仍可退",
            "转人工审核（中风险，风险分 45）；主因：已签收、高价值订单",
            needs_evidence=True,
        ),
        review_history=[{
            "action": "APPROVE", "action_label": "审批通过", "at": "2026-10-28 11:00:00",
            "by_id": "m_2001", "by": "陈明", "note": "协商退 1000 元",
            "amount_cents": 100000,
        }],
    ),
    # 破损全额退
    dict(
        refund_id="RF20261004093000D5E6", order_id="ORD20260928010",
        user_id="u_1002", user_name="李娜", amount_cents=89900,
        requested_cents=None, issue_type="DAMAGED",
        reason="收到时玻璃壶身有裂纹，无法使用", status="APPROVED", mode="MANUAL",
        seq=4, created_at="2026-10-03 09:10:00", reviewed_at="2026-10-04 09:30:00",
        reviewer_id="m_2001", reviewer_name="陈明", review_note="照片确认破损，全额退款",
        decision=_decision(
            "HIGH", 35, "P0",
            [_rule("R12", "已签收", 20, "已签收，需核验退货入库或凭证"),
             _rule("R21", "需凭证类问题", 5, "商品破损，人工需核验图片或检测报告"),
             _rule("R23", "已签收后全额退", 15, "已签收且退全部余额，需确认退货实物")],
            "用户未指定金额，按可退余额全额退",
            "转人工审核（高风险，风险分 35）；主因：已签收、已签收后全额退",
            needs_evidence=True,
        ),
        review_history=[{
            "action": "APPROVE", "action_label": "审批通过", "at": "2026-10-04 09:30:00",
            "by_id": "m_2001", "by": "陈明", "note": "照片确认破损，全额退款",
            "amount_cents": 89900,
        }],
    ),
    # 同订单第二次部分退款（异常：MULTI_REFUND）
    dict(
        refund_id="RF20261024100000F7A8", order_id="ORD20261005012",
        user_id="u_1003", user_name="王强", amount_cents=50000,
        requested_cents=50000, issue_type="QUALITY",
        reason="开机有异响，接受部分补偿", status="APPROVED", mode="MANUAL",
        seq=5, created_at="2026-10-23 10:00:00", reviewed_at="2026-10-24 10:00:00",
        reviewer_id="m_2001", reviewer_name="陈明", review_note="客户接受补偿方案",
        decision=_decision(
            "MEDIUM", 45, "P1",
            [_rule("R12", "已签收", 20, "已签收，需核验退货入库或凭证"),
             _rule("R13", "高价值订单", 20, "实付 ¥3,299.00，达高价值阈值"),
             _rule("R21", "需凭证类问题", 5, "质量问题，人工需核验图片或检测报告")],
            "用户主张部分退款，按主张金额执行，剩余 ¥2,799.00 仍可退",
            "转人工审核（中风险，风险分 45）；主因：已签收、高价值订单",
            needs_evidence=True,
        ),
        review_history=[{
            "action": "APPROVE", "action_label": "审批通过", "at": "2026-10-24 10:00:00",
            "by_id": "m_2001", "by": "陈明", "note": "客户接受补偿方案",
            "amount_cents": 50000,
        }],
    ),
    dict(
        refund_id="RF20261025100000G9B1", order_id="ORD20261005012",
        user_id="u_1003", user_name="王强", amount_cents=30000,
        requested_cents=30000, issue_type="QUALITY",
        reason="异响仍然存在，再退 300", status="APPROVED", mode="MANUAL",
        seq=6, created_at="2026-10-24 20:00:00", reviewed_at="2026-10-25 10:00:00",
        reviewer_id="m_2001", reviewer_name="陈明", review_note="二次补偿，注意同类问题",
        decision=_decision(
            "HIGH", 65, "P0",
            [_rule("R12", "已签收", 20, "已签收，需核验退货入库或凭证"),
             _rule("R13", "高价值订单", 20, "实付 ¥3,299.00，达高价值阈值"),
             _rule("R16", "非首次退款", 20, "该订单此前已退 ¥500.00，属二次及以上申请"),
             _rule("R21", "需凭证类问题", 5, "质量问题，人工需核验图片或检测报告")],
            "用户主张部分退款，按主张金额执行，剩余 ¥2,499.00 仍可退",
            "转人工审核（高风险，风险分 65）；主因：已签收、高价值订单",
            needs_evidence=True,
        ),
        review_history=[{
            "action": "APPROVE", "action_label": "审批通过", "at": "2026-10-25 10:00:00",
            "by_id": "m_2001", "by": "陈明", "note": "二次补偿，注意同类问题",
            "amount_cents": 30000,
        }],
    ),
    # 被驳回 -> 订单锁定
    dict(
        refund_id="RF20261023153000H1J2", order_id="ORD20261020011",
        user_id="u_1003", user_name="王强", amount_cents=19900,
        requested_cents=None, issue_type="NO_LONGER_NEEDED",
        reason="买完发现用不上，想退掉", status="REJECTED", mode="MANUAL",
        seq=7, created_at="2026-10-23 15:30:00", reviewed_at="2026-10-23 18:00:00",
        reviewer_id="m_2001", reviewer_name="陈明",
        review_note="已签收超过 7 天且为无理由退货，按政策不予退款",
        decision=_decision(
            "HIGH", 60, "P0",
            [_rule("R12", "已签收", 20, "已签收，需核验退货入库或凭证"),
             _rule("R20", "签收超 7 天", 15, "签收已 1 天以内")],
            "用户未指定金额，按可退余额全额退",
            "转人工审核（高风险，风险分 60）；主因：已签收、签收超 7 天",
        ),
        review_history=[{
            "action": "REJECT", "action_label": "驳回申请", "at": "2026-10-23 18:00:00",
            "by_id": "m_2001", "by": "陈明",
            "note": "已签收超过 7 天且为无理由退货，按政策不予退款",
            "amount_cents": 19900,
        }],
    ),
    # 待审批：后台一打开就能看到
    dict(
        refund_id="RF20261106093000E5F6", order_id="ORD20261010007",
        user_id="u_1002", user_name="李娜", amount_cents=50000,
        requested_cents=50000, issue_type="QUALITY",
        reason="触控偶尔失灵，希望再退 500 元", status="PENDING", mode="MANUAL",
        seq=8, created_at="2026-11-06 09:30:00", reviewed_at=None,
        reviewer_id=None, reviewer_name=None, review_note=None,
        decision=_decision(
            "MEDIUM", 65, "P1",
            [_rule("R12", "已签收", 20, "已签收，需核验退货入库或凭证"),
             _rule("R13", "高价值订单", 20, "实付 ¥4,799.00，达高价值阈值"),
             _rule("R16", "非首次退款", 20, "该订单此前已退 ¥1,000.00，属二次及以上申请"),
             _rule("R21", "需凭证类问题", 5, "质量问题，人工需核验图片或检测报告")],
            "用户主张部分退款，按主张金额执行，剩余 ¥3,299.00 仍可退",
            "转人工审核（中风险，风险分 65）；主因：已签收、高价值订单",
            needs_evidence=True,
        ),
        review_history=[],
    ),
]

# 驳回产生的锁
LOCKS = {
    "ORD20261020011": {
        "order_id": "ORD20261020011",
        "refund_id": "RF20261023153000H1J2",
        "locked_at": "2026-10-23 18:00:00",
        "reason": "已签收超过 7 天且为无理由退货，按政策不予退款",
        "locked_by": "陈明",
        "active": True,
    }
}


def _demo_session() -> dict:
    """一段预置的网页会话，用于让会话页与审计页开箱有内容。

    内容是固定的，不调用模型——seed 必须可重复，跑两次得到同样的字节。
    """
    steps = [
        {"type": "tool_call", "name": "query_after_sales",
         "args": {"order_id": "ORD20261010007"},
         "text": '{"order_id": "ORD20261010007"}'},
        {"type": "tool_result", "name": "query_after_sales", "status": "OK",
         "text": "共 3 条售后记录：已通过 1 笔（合计 ¥1,000.00）、待审批 1 笔、已驳回 0 笔",
         "summary": "共 3 条售后记录", "raw": "{\"ok\": true, \"count\": 3}"},
        {"type": "reply",
         "text": "李娜您好，iPad 这笔目前有一张 ¥500 的退款申请在排队，是第 1 位，还在等商家审核。"
                 "这单之前已经退过 ¥1,000，实付 ¥4,799，所以最多还能退 ¥3,799。"},
    ]
    return {
        "session_id": "demo-web-u_1002",
        "user_id": "u_1002",
        "user_name": "李娜",
        "channel": "WEB",
        "seq": 1,
        "created_at": "2026-11-06 09:35:00",
        "updated_at": "2026-11-06 09:35:00",
        "title": "我这单退款到哪一步了",
        "messages": [
            {"role": "user", "content": "我这单退款到哪一步了？ORD20261010007",
             "steps": [], "ts": "2026-11-06 09:35:00"},
            {"role": "assistant",
             "content": steps[-1]["text"], "steps": steps,
             "ts": "2026-11-06 09:35:04"},
        ],
    }


def _demo_audit(session_id: str) -> dict:
    """与演示会话对应的一串审计事件，时间戳固定。"""
    sid = session_id
    rows = [
        (audit.KIND_USER_MESSAGE, "OK", {"text": "我这单退款到哪一步了？ORD20261010007", "turn": 1}),
        (audit.KIND_TOOL_CALL, "OK", {"tool": "query_after_sales",
                                      "args": {"order_id": "ORD20261010007"}}),
        (audit.KIND_TOOL_RESULT, "OK", {"tool": "query_after_sales",
                                        "text": "共 3 条售后记录：已通过 1 笔、待审批 1 笔",
                                        "code": None}),
        (audit.KIND_AGENT_REPLY, "OK", {"text": "李娜您好，iPad 这笔目前有一张 ¥500 的退款申请在排队……"}),
        (audit.KIND_RULE_DECISION, "OK", {
            "order_id": "ORD20261010007",
            "decision": "MANUAL_REVIEW", "decision_label": "转人工审核",
            "risk_level": "MEDIUM", "risk_score": 65, "priority": "P1",
            "amount_cents": 50000,
            "amount_reason": "用户主张部分退款，按主张金额执行，剩余 ¥3,299.00 仍可退",
            "issue_type": "QUALITY",
            "summary": "转人工审核（中风险，风险分 65）；主因：已签收、高价值订单",
        }),
        (audit.KIND_HUMAN_ACTION, "OK", {
            "action": "APPROVE", "action_label": "审批通过",
            "refund_id": "RF20261028110000C3D4", "order_id": "ORD20261010007",
            "amount_cents": 100000, "note": "协商退 1000 元",
            "agent_decision": "转人工审核（中风险，风险分 45）",
        }),
    ]
    events = {}
    # 时间从 09:35 起每条 +1 秒
    base_h, base_m, base_s = 9, 35, 0
    for idx, (kind, status, payload) in enumerate(rows, start=1):
        sec = base_s + idx
        ts = f"2026-11-06 {base_h:02d}:{base_m + sec // 60:02d}:{sec % 60:02d}"
        event_id = f"EV{idx:06d}A{idx:03d}"
        events[event_id] = {
            "event_id": event_id, "seq": idx, "ts": ts, "kind": kind,
            "kind_label": audit.KIND_LABEL[kind], "status": status,
            "channel": audit.CHANNEL_WEB,
            "session_id": sid if idx <= 4 else None,
            "user_id": "u_1002" if idx <= 4 else "m_2001",
            "user_name": "李娜" if idx <= 4 else "陈明",
            "elapsed_ms": 4200 if kind == audit.KIND_AGENT_REPLY else None,
            "payload": payload,
        }
    return {"schema_version": store.SCHEMA_VERSION, "events": events, "seq": len(rows)}


def seed(target_dir: Path | None = None) -> Path:
    """重建数据目录，返回目录路径。"""
    directory = Path(target_dir) if target_dir else store.data_dir()
    directory.mkdir(parents=True, exist_ok=True)

    for name in (store.USERS_FILE, store.ORDERS_FILE, store.REFUNDS_FILE,
                 store.AUDIT_FILE, store.SESSIONS_FILE):
        for suffix in ("", ".lock"):
            p = directory / (name + suffix)
            if p.exists():
                p.unlink()

    users = store.empty_users()
    for u in USERS:
        users["users"][u["user_id"]] = u

    orders = store.empty_orders()
    for o in ORDERS:
        orders["orders"][o["order_id"]] = o

    refunds = store.empty_refunds()
    for r in REFUNDS:
        refunds["refunds"][r["refund_id"]] = r
        refunds["seq"] = max(refunds["seq"], r["seq"])
    refunds["locks"] = dict(LOCKS)

    store._write_atomic(directory / store.USERS_FILE, users)
    store._write_atomic(directory / store.ORDERS_FILE, orders)
    store._write_atomic(directory / store.REFUNDS_FILE, refunds)

    session = _demo_session()
    sessions = store.empty_sessions()
    sessions["sessions"][session["session_id"]] = session
    sessions["seq"] = 1
    store._write_atomic(directory / store.SESSIONS_FILE, sessions)
    store._write_atomic(directory / store.AUDIT_FILE, _demo_audit(session["session_id"]))
    return directory


def main() -> None:
    directory = seed()
    print(f"数据已重建: {directory}")
    print(f"  用户   {len(USERS)} 个（3 顾客 + 1 商家）")
    print(f"  订单   {len(ORDERS)} 笔")
    print(f"  退款   {len(REFUNDS)} 条（1 条待审批，1 条已驳回并锁定）")
    print("  审计 / 会话  已重建（含 1 段演示会话）")
    print()
    print("  演示账号：")
    print(f"    zhangwei  / {CUSTOMER_PASSWORD}   张伟（顾客）")
    print(f"    lina      / {CUSTOMER_PASSWORD}   李娜（顾客）")
    print(f"    wangqiang / {CUSTOMER_PASSWORD}   王强（顾客）")
    print(f"    merchant  / {MERCHANT_PASSWORD}   陈明（商家，后台）")


if __name__ == "__main__":
    main()
