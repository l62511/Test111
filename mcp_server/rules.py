"""退款规则引擎：把"该不该退、退多少、要不要人工看"变成可复算的判定。

为什么要单独一层，而不是让模型自己判断：

1. **金额不能由模型决定**。模型看到的数字是用户说的，用户可能顺口报个大数，
   也可能被提示词带偏。退款金额只能由服务端按订单实付、已退金额、
   政策上限算出来，模型只负责把结果翻译成话术。
2. **判定要可解释**。人工审核中心需要展示"为什么这单被转人工"，
   不能只有一句"模型觉得有风险"。所以每一次判定都产出命中规则清单，
   每条含规则编号、名称、加减分、具体说明。
3. **判定要可复算**。同样的订单、同样的历史，任何时候算出来必须一致。
   这里不做随机、不调模型，纯函数。

判定分三档：

    AUTO_APPROVE   未发货的低风险单，直接放款，进商家后台只留记录不占队列
    MANUAL_REVIEW  已发货/已签收，或风险分偏高，进人工队列
    REJECT         结构性不合规（状态不可退、无余额、超上限），直接拒绝

风险分只影响 MANUAL_REVIEW 的**优先级**和展示，不改变"发货后必须人工"这条硬规则。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# 决策
DECISION_AUTO = "AUTO_APPROVE"
DECISION_MANUAL = "MANUAL_REVIEW"
DECISION_REJECT = "REJECT"

DECISION_LABEL = {
    DECISION_AUTO: "自动放款",
    DECISION_MANUAL: "转人工审核",
    DECISION_REJECT: "拒绝",
}

# 风险等级
RISK_LOW = "LOW"
RISK_MEDIUM = "MEDIUM"
RISK_HIGH = "HIGH"

RISK_LABEL = {RISK_LOW: "低风险", RISK_MEDIUM: "中风险", RISK_HIGH: "高风险"}

# 退款问题类型。模型只从这几种里选，不允许自由发挥。
ISSUE_TYPES = (
    "NOT_RECEIVED",       # 没收到货
    "DAMAGED",            # 收到时已破损
    "NOT_AS_DESCRIBED",   # 与描述不符
    "QUALITY",            # 质量问题
    "WRONG_ITEM",         # 拍错 / 发错货
    "NO_LONGER_NEEDED",   # 不想要了
    "OTHER",
)

ISSUE_LABEL = {
    "NOT_RECEIVED": "未收到货",
    "DAMAGED": "商品破损",
    "NOT_AS_DESCRIBED": "与描述不符",
    "QUALITY": "质量问题",
    "WRONG_ITEM": "拍错/发错",
    "NO_LONGER_NEEDED": "不想要了",
    "OTHER": "其他原因",
}

# 需要用户提供凭证的问题类型，人工审核时要重点看
NEEDS_EVIDENCE = ("DAMAGED", "NOT_AS_DESCRIBED", "QUALITY")

# 高价值阈值（分）
HIGH_VALUE_CENTS = 300_000      # 3000 元
MID_VALUE_CENTS = 100_000       # 1000 元
SMALL_AMOUNT_CENTS = 10_000     # 100 元

# 签收后多久算"久了"
LATE_DAYS = 7
VERY_LATE_DAYS = 30

# 转入高风险的分值门槛
HIGH_SCORE = 30
MEDIUM_SCORE = 10


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _days_since(ts: str | None, now: datetime) -> int | None:
    start = _parse(ts)
    if start is None:
        return None
    return max(0, (now - start).days)


def _rule(rule_id: str, name: str, delta: int, detail: str) -> dict[str, Any]:
    return {"id": rule_id, "name": name, "delta": delta, "detail": detail}


def evaluate(
    order: dict[str, Any],
    *,
    requested_cents: int | None = None,
    issue_type: str = "OTHER",
    user_refunds: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """对一笔已通过结构性校验的订单做退款判定。

    调用前 service 层已确认：订单属于本人、状态可退、没有待审批申请、未被锁定。
    这里只负责风险与金额，不重复那些结构校验。
    """
    now = now or datetime.now()
    user_refunds = user_refunds or []
    issue_type = issue_type if issue_type in ISSUE_TYPES else "OTHER"

    amount_cents = order["amount_cents"]
    refunded = order.get("refunded_cents", 0)
    balance = amount_cents - refunded

    shipped = order.get("shipped_at") is not None
    delivered = order.get("delivered_at") is not None
    days_after_delivery = _days_since(order.get("delivered_at"), now)

    rules: list[dict[str, Any]] = []
    score = 0

    # ---------- 结构性拒绝 ----------
    if balance <= 0:
        return _reject(order, "NO_BALANCE", "订单已全额退款，没有可退余额",
                       rules, score, amount_cents, issue_type)
    if requested_cents is not None and requested_cents > balance:
        return _reject(order, "AMOUNT_OVER_BALANCE",
                       f"申请金额超过可退余额（余额 {balance} 分）",
                       rules, score, amount_cents, issue_type)

    # ---------- 金额：由引擎算，不采信模型 ----------
    # 用户没报金额 = 退剩余全部；报了且不超余额 = 按用户主张的部分退。
    decided = balance if requested_cents is None else requested_cents
    if requested_cents is None:
        amount_reason = f"用户未指定金额，按可退余额全额退（实付扣除已退 {refunded} 分）"
    elif requested_cents == balance:
        amount_reason = "用户主张金额等于可退余额"
    else:
        amount_reason = f"用户主张部分退款，按主张金额执行，剩余 {balance - decided} 分仍可退"

    # ---------- 物流节点 ----------
    if not shipped:
        score -= 35
        rules.append(_rule("R10", "未发货", -35,
                          "订单未发货，不涉及退货验货，资金风险最低"))
    elif not delivered:
        score += 10
        rules.append(_rule("R11", "已发货未签收", 10,
                          "货在途中，需确认截单或拒收情况"))
    else:
        score += 20
        rules.append(_rule("R12", "已签收", 20,
                          "已签收，需核验退货入库或凭证"))

    # ---------- 金额规模 ----------
    if amount_cents >= HIGH_VALUE_CENTS:
        score += 20
        rules.append(_rule("R13", "高价值订单", 20,
                          f"实付 ¥{amount_cents / 100:,.2f}，达高价值阈值"))
    elif amount_cents >= MID_VALUE_CENTS:
        score += 10
        rules.append(_rule("R14", "中大额订单", 10,
                          f"实付 ¥{amount_cents / 100:,.2f}"))

    if decided <= SMALL_AMOUNT_CENTS:
        score -= 10
        rules.append(_rule("R15", "小额退款", -10,
                          f"本次 ¥{decided / 100:,.2f}，争议成本低于处理成本"))

    # ---------- 历史行为 ----------
    if refunded > 0:
        score += 20
        rules.append(_rule("R16", "非首次退款", 20,
                          f"该订单此前已退 ¥{refunded / 100:,.2f}，属二次及以上申请"))

    recent = [r for r in user_refunds
              if (_days_since(r.get("created_at"), now) or 9999) <= 30]
    if len(recent) >= 2:
        score += 15
        rules.append(_rule("R17", "近期退款频繁", 15,
                          f"近 30 天已有 {len(recent)} 笔退款申请"))
    elif len(user_refunds) == 0:
        score -= 10
        rules.append(_rule("R18", "首次售后", -10,
                          "该用户此前没有退款记录"))

    # ---------- 时效 ----------
    if delivered and days_after_delivery is not None:
        if days_after_delivery > VERY_LATE_DAYS:
            score += 30
            rules.append(_rule("R19", "签收过久", 30,
                              f"签收已 {days_after_delivery} 天，超过 {VERY_LATE_DAYS} 天"))
        elif days_after_delivery > LATE_DAYS:
            score += 15
            rules.append(_rule("R20", "签收超 7 天", 15,
                              f"签收已 {days_after_delivery} 天"))

    # ---------- 问题类型 ----------
    if issue_type in NEEDS_EVIDENCE:
        score += 5
        rules.append(_rule("R21", "需凭证类问题", 5,
                          f"{ISSUE_LABEL[issue_type]}，人工需核验图片或检测报告"))
    if issue_type == "NOT_RECEIVED" and delivered:
        score += 15
        rules.append(_rule("R22", "声称未收货但已签收", 15,
                          "物流显示已签收，与用户描述矛盾，需核对签收凭证"))

    # ---------- 全额退款 ----------
    if decided >= balance and delivered:
        score += 15
        rules.append(_rule("R23", "已签收后全额退", 15,
                          "已签收且退全部余额，需确认退货实物"))

    # ---------- 汇总 ----------
    score = max(0, min(100, score))
    if score >= HIGH_SCORE:
        risk = RISK_HIGH
    elif score >= MEDIUM_SCORE:
        risk = RISK_MEDIUM
    else:
        risk = RISK_LOW

    # 硬规则：只要发过货，一律人工。未发货才能自动放款。
    if shipped:
        decision = DECISION_MANUAL
    elif score >= HIGH_SCORE:
        decision = DECISION_MANUAL
    else:
        decision = DECISION_AUTO

    priority = "P0" if risk == RISK_HIGH else ("P1" if risk == RISK_MEDIUM else "P2")

    return {
        "decision": decision,
        "decision_label": DECISION_LABEL[decision],
        "risk_level": risk,
        "risk_label": RISK_LABEL[risk],
        "risk_score": score,
        "priority": priority,
        "amount_cents": decided,
        "amount_reason": amount_reason,
        "balance_cents": balance,
        "issue_type": issue_type,
        "issue_label": ISSUE_LABEL[issue_type],
        "rules": rules,
        "needs_evidence": issue_type in NEEDS_EVIDENCE,
        "summary": _summary(decision, risk, score, rules),
    }


def _summary(decision: str, risk: str, score: int, rules: list[dict[str, Any]]) -> str:
    """给人工审核看的一句话结论。"""
    head = f"{DECISION_LABEL[decision]}（{RISK_LABEL[risk]}，风险分 {score}）"
    hits = [r for r in rules if r["delta"] > 0]
    if not hits:
        return head + "；无风险项命中"
    top = sorted(hits, key=lambda r: -r["delta"])[:2]
    return head + "；主因：" + "、".join(r["name"] for r in top)


def _reject(order, code, message, rules, score, amount_cents, issue_type) -> dict[str, Any]:
    return {
        "decision": DECISION_REJECT,
        "decision_label": DECISION_LABEL[DECISION_REJECT],
        "risk_level": RISK_HIGH,
        "risk_label": RISK_LABEL[RISK_HIGH],
        "risk_score": 100,
        "priority": "P0",
        "reject_code": code,
        "reject_reason": message,
        "amount_cents": 0,
        "amount_reason": message,
        "balance_cents": order["amount_cents"] - order.get("refunded_cents", 0),
        "issue_type": issue_type,
        "issue_label": ISSUE_LABEL[issue_type],
        "rules": rules,
        "needs_evidence": False,
        "summary": f"拒绝（{message}）",
    }


def explain(decision: dict[str, Any]) -> str:
    """把判定结果铺成多行文本，CLI 与审计日志复用。"""
    lines = [
        f"判定: {decision['decision_label']}  风险: {decision['risk_label']}"
        f"（{decision['risk_score']} 分）  优先级: {decision['priority']}",
        f"金额: ¥{decision['amount_cents'] / 100:,.2f}  —— {decision['amount_reason']}",
    ]
    for r in decision["rules"]:
        sign = "+" if r["delta"] > 0 else ""
        lines.append(f"  [{r['id']}] {r['name']} {sign}{r['delta']}  {r['detail']}")
    return "\n".join(lines)
