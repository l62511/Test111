"""售后数据分析：把订单与退款记录聚合成商家能看懂的指标。

口径统一写在最前面，避免"退款率"各说各话：

- **退款金额**：以「已通过」的退款记录为准，统计实际放出去的钱。
  待审批的单列，不算进已退金额——钱还没出去。
- **退款率**：有过退款通过的订单数 / 同期订单总数。分母是订单数不是金额，
  因为金额会被单价拉偏（一台咖啡机能顶十副耳机）。
- **退款原因分布**：按退款记录上的 issue_type 归类，老数据没有该字段时
  按 reason 文本兜底归到 OTHER。
- **商品退款排行**：按商品名聚合，输出退款金额、笔数、该商品的退款率。
- **异常订单**：规则见 ANOMALY_RULES，每条都给出可核对的依据，不做黑箱打分。

时间范围以退款记录的 created_at（提交时间）为准，不是审批时间——
商家关心的是"这周用户提了多少"，而不是"这周我批了多少"。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from . import rules, store

PERIODS = {
    "WEEK": "本周",
    "MONTH": "本月",
    "ALL": "全部",
}

# 异常类型
ANOMALY_LABEL = {
    "MULTI_REFUND": "同一订单多次退款",
    "FULL_AFTER_DELIVERY": "签收后全额退款",
    "REPEAT_APPLY": "驳回后重复申请",
    "FREQUENT_USER": "短期内频繁申请",
    "LATE_CLAIM": "签收很久才申请",
    "STALE_PENDING": "待审批积压超 24 小时",
}


def _now() -> datetime:
    return datetime.now()


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def period_range(period: str, now: datetime | None = None) -> tuple[datetime | None, str]:
    """返回 (起始时间, 展示标签)。ALL 返回 None 表示不限制。"""
    now = now or _now()
    key = (period or "ALL").upper()
    if key == "WEEK":
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return start, f"本周（{start:%m-%d} 起）"
    if key == "MONTH":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, f"本月（{start:%m-%d} 起）"
    return None, "全部时间"


def _load() -> tuple[dict[str, Any], dict[str, Any]]:
    orders = store.read_only(store.ORDERS_FILE, store.empty_orders())["orders"]
    refunds = store.read_only(store.REFUNDS_FILE, store.empty_refunds())["refunds"]
    return orders, refunds


def _issue_of(refund: dict[str, Any]) -> str:
    issue = refund.get("issue_type")
    if issue in rules.ISSUE_TYPES:
        return issue
    return "OTHER"


def _cents_to_yuan(cents: int) -> str:
    return f"¥{cents / 100:,.2f}"


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def summary(period: str = "ALL", top_n: int = 8) -> dict[str, Any]:
    start, label = period_range(period)
    orders, refunds = _load()
    now = _now()

    rows = list(refunds.values())
    in_range = [r for r in rows if start is None or (_parse(r.get("created_at")) or now) >= start]

    approved = [r for r in in_range if r["status"] == "APPROVED"]
    pending = [r for r in in_range if r["status"] == "PENDING"]
    rejected = [r for r in in_range if r["status"] == "REJECTED"]

    approved_amount = sum(r["amount_cents"] for r in approved)
    pending_amount = sum(r["amount_cents"] for r in pending)

    # 订单口径：期内的订单（按创建时间）
    period_orders = [
        o for o in orders.values()
        if start is None or (_parse(o.get("created_at")) or now) >= start
    ]
    refunded_order_ids = {r["order_id"] for r in approved}
    refunded_order_count = len([o for o in period_orders if o["order_id"] in refunded_order_ids])
    order_total = len(period_orders)
    refund_rate = round(refunded_order_count / order_total * 100, 2) if order_total else 0.0

    # 金额口径的退款率：只统计期内订单实付
    period_gmv = sum(o["amount_cents"] for o in period_orders)
    amount_rate = round(approved_amount / period_gmv * 100, 2) if period_gmv else 0.0

    return {
        "period": {"key": (period or "ALL").upper(), "label": label},
        "totals": {
            "refund_amount_cents": approved_amount,
            "refund_amount": _cents_to_yuan(approved_amount),
            "refund_count": len(approved),
            "pending_amount_cents": pending_amount,
            "pending_amount": _cents_to_yuan(pending_amount),
            "pending_count": len(pending),
            "rejected_count": len(rejected),
            "order_count": order_total,
            "refunded_order_count": refunded_order_count,
            "refund_rate": refund_rate,
            "amount_rate": amount_rate,
            "gmv": _cents_to_yuan(period_gmv),
            "avg_refund": _cents_to_yuan(
                approved_amount // len(approved) if approved else 0
            ),
        },
        "by_issue": _by_issue(in_range),
        "by_product": _by_product(orders, approved, period_orders, top_n),
        "anomalies": anomalies(period),
        "trend": _trend(approved),
        "human_vs_auto": {
            "auto": len([r for r in approved if r.get("mode") == "AUTO"]),
            "human": len([r for r in approved if r.get("mode") != "AUTO"]),
        },
    }


def _by_issue(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total = len(rows) or 1
    buckets: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = _issue_of(r)
        b = buckets.setdefault(key, {"issue": key, "count": 0, "amount_cents": 0})
        b["count"] += 1
        b["amount_cents"] += r["amount_cents"]
    out = []
    for b in buckets.values():
        b["label"] = rules.ISSUE_LABEL[b["issue"]]
        b["amount"] = _cents_to_yuan(b["amount_cents"])
        b["share"] = round(b["count"] / total * 100, 1)
        out.append(b)
    out.sort(key=lambda b: -b["count"])
    return out


def _by_product(
    orders: dict[str, Any],
    approved: list[dict[str, Any]],
    period_orders: list[dict[str, Any]],
    top_n: int,
) -> list[dict[str, Any]]:
    """商品退款排行：按退款金额降序。"""
    agg: dict[str, dict[str, Any]] = {}
    for r in approved:
        order = orders.get(r["order_id"], {})
        name = order.get("item_name", "未知商品")
        item = agg.setdefault(name, {
            "item_name": name, "amount_cents": 0, "count": 0, "order_ids": set(),
        })
        item["amount_cents"] += r["amount_cents"]
        item["count"] += 1
        item["order_ids"].add(r["order_id"])

    # 该商品在期内的订单数，用来算单商品退款率
    sold: dict[str, int] = {}
    for o in period_orders:
        sold[o["item_name"]] = sold.get(o["item_name"], 0) + 1

    out = []
    for rank, item in enumerate(
        sorted(agg.values(), key=lambda i: -i["amount_cents"])[:top_n], start=1
    ):
        n_sold = sold.get(item["item_name"], 0)
        out.append({
            "rank": rank,
            "item_name": item["item_name"],
            "amount_cents": item["amount_cents"],
            "amount": _cents_to_yuan(item["amount_cents"]),
            "count": item["count"],
            "orders": len(item["order_ids"]),
            "sold": n_sold,
            "rate": round(len(item["order_ids"]) / n_sold * 100, 1) if n_sold else None,
        })
    return out


def _trend(approved: list[dict[str, Any]], days: int = 14) -> list[dict[str, Any]]:
    """最近 N 天的每日退款金额，用于折线图。"""
    today = _now().date()
    buckets = {(today - timedelta(days=i)).isoformat(): 0 for i in range(days - 1, -1, -1)}
    counts = {k: 0 for k in buckets}
    for r in approved:
        day = (r.get("created_at") or "")[:10]
        if day in buckets:
            buckets[day] += r["amount_cents"]
            counts[day] += 1
    return [
        {"date": d, "label": d[5:], "amount_cents": v, "count": counts[d]}
        for d, v in buckets.items()
    ]


# --------------------------------------------------------------------------
# 异常订单
# --------------------------------------------------------------------------
def anomalies(period: str = "ALL", limit: int = 30) -> list[dict[str, Any]]:
    """按可核对的规则找异常，每条都带依据。"""
    start, _ = period_range(period)
    orders, refunds = _load()
    now = _now()
    rows = [r for r in refunds.values()
            if start is None or (_parse(r.get("created_at")) or now) >= start]

    out: list[dict[str, Any]] = []

    by_order: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_order.setdefault(r["order_id"], []).append(r)

    for order_id, rs in by_order.items():
        order = orders.get(order_id, {})
        name = order.get("item_name", "未知商品")
        approved = [r for r in rs if r["status"] == "APPROVED"]

        if len(approved) >= 2:
            out.append({
                "type": "MULTI_REFUND",
                "label": ANOMALY_LABEL["MULTI_REFUND"],
                "order_id": order_id,
                "item_name": name,
                "level": "HIGH",
                "detail": f"该订单已通过 {len(approved)} 笔退款，合计 "
                          f"{_cents_to_yuan(sum(r['amount_cents'] for r in approved))}",
            })

        if order.get("delivered_at") and approved:
            refunded = order.get("refunded_cents", 0)
            if refunded >= order["amount_cents"] and order["amount_cents"] > 0:
                out.append({
                    "type": "FULL_AFTER_DELIVERY",
                    "label": ANOMALY_LABEL["FULL_AFTER_DELIVERY"],
                    "order_id": order_id,
                    "item_name": name,
                    "level": "MEDIUM",
                    "detail": f"已签收后全额退款 {_cents_to_yuan(refunded)}，需确认退货入库",
                })

        if any(r["status"] == "REJECTED" for r in rs) and \
                any(r["status"] in ("PENDING", "APPROVED") for r in rs):
            out.append({
                "type": "REPEAT_APPLY",
                "label": ANOMALY_LABEL["REPEAT_APPLY"],
                "order_id": order_id,
                "item_name": name,
                "level": "HIGH",
                "detail": "驳回后仍产生新申请，检查是否走了解锁流程",
            })

        if order.get("delivered_at"):
            first = min((r.get("created_at") or "") for r in rs)
            d = _parse(order["delivered_at"])
            f = _parse(first)
            if d and f and (f - d).days > 30:
                out.append({
                    "type": "LATE_CLAIM",
                    "label": ANOMALY_LABEL["LATE_CLAIM"],
                    "order_id": order_id,
                    "item_name": name,
                    "level": "MEDIUM",
                    "detail": f"签收后 {(f - d).days} 天才申请退款",
                })

    # 用户维度：7 天内 >= 2 次
    by_user: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r["status"] in ("APPROVED", "PENDING"):
            by_user.setdefault(r["user_id"], []).append(r)
    for uid, rs in by_user.items():
        recent = [r for r in rs if (_parse(r.get("created_at")) or now) >= now - timedelta(days=7)]
        if len(recent) >= 2:
            out.append({
                "type": "FREQUENT_USER",
                "label": ANOMALY_LABEL["FREQUENT_USER"],
                "order_id": "、".join(sorted({r["order_id"] for r in recent})),
                "item_name": rs[0].get("user_name", uid),
                "level": "MEDIUM",
                "detail": f"用户 {rs[0].get('user_name', uid)} 近 7 天提交 {len(recent)} 笔退款",
            })

    # 积压
    for r in rows:
        if r["status"] != "PENDING":
            continue
        created = _parse(r.get("created_at"))
        if created and (now - created).total_seconds() > 86400:
            out.append({
                "type": "STALE_PENDING",
                "label": ANOMALY_LABEL["STALE_PENDING"],
                "order_id": r["order_id"],
                "item_name": orders.get(r["order_id"], {}).get("item_name", "未知商品"),
                "level": "HIGH",
                "detail": f"待审批已 {(now - created).days} 天，申请编号 {r['refund_id']}",
            })

    order_level = {"HIGH": 0, "MEDIUM": 1}
    out.sort(key=lambda a: order_level.get(a["level"], 2))
    return out[:limit]


def to_text(data: dict[str, Any]) -> str:
    """把汇总铺成便于模型转述的紧凑文本。"""
    t = data["totals"]
    lines = [
        f"【{data['period']['label']}】",
        f"退款金额 {t['refund_amount']}（{t['refund_count']} 笔，客单价 {t['avg_refund']}）",
        f"退款率 {t['refund_rate']}%（{t['refunded_order_count']}/{t['order_count']} 单）"
        f"，金额占比 {t['amount_rate']}%",
        f"待审批 {t['pending_count']} 笔 / {t['pending_amount']}，已驳回 {t['rejected_count']} 笔",
        f"自动放款 {data['human_vs_auto']['auto']} 笔，人工放款 {data['human_vs_auto']['human']} 笔",
    ]
    if data["by_product"]:
        lines.append("商品退款排行：")
        for p in data["by_product"][:5]:
            rate = f"，退款率 {p['rate']}%" if p["rate"] is not None else ""
            lines.append(f"  {p['rank']}. {p['item_name']} {p['amount']}（{p['count']} 笔{rate}）")
    if data["by_issue"]:
        lines.append("原因分布：")
        for i in data["by_issue"]:
            lines.append(f"  - {i['label']} {i['count']} 笔 / {i['amount']}（{i['share']}%）")
    if data["anomalies"]:
        lines.append(f"异常 {len(data['anomalies'])} 条，靠前的：")
        for a in data["anomalies"][:3]:
            lines.append(f"  - [{a['label']}] {a['detail']}")
    return "\n".join(lines)
