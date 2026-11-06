"""协议层冒烟测试：完全走 MCP 协议调用五个工具，不经过大模型。

验证的是「Agent 之外」这一整条链路是否真的按规则工作：
    MCP Client -> stdio -> MCP Server -> 规则引擎/分析/审计 -> JSON 存储

用法:
    python scripts/smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from data.seed import seed  # noqa: E402

UID = "u_1001"
MERCHANT = "m_2001"

PAID = "ORD20261028001"             # 已支付，未发货
UNSHIPPED = "ORD20261015002"        # 已取消，未发货 -> 自动退
SHIPPED = "ORD20261008009"          # 已取消，已发货 -> 需审批
COMPLETED = "ORD20261102005"        # 已完成，已签收 -> 需审批
OTHERS = "ORD20261103006"           # 李娜的订单

passed = 0
failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label}")
        if detail:
            print(f"         {detail[:300]}")


def parse(raw):
    """MCP 工具返回的是 content 列表，取出其中的 JSON。"""
    if isinstance(raw, list):
        text = next(
            (c.get("text", "") for c in raw
             if isinstance(c, dict) and c.get("type") == "text"),
            "",
        )
    else:
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"_raw": text}


def schema_of(tool) -> dict:
    """args_schema 可能是 pydantic 模型，也可能已经是 dict。"""
    schema = tool.args_schema
    if isinstance(schema, dict):
        return schema
    if hasattr(schema, "model_json_schema"):
        return schema.model_json_schema()
    return {}


async def main() -> None:
    tmp = Path(tempfile.mkdtemp()) / "data"
    seed(tmp)
    os.environ["ORDER_DATA_DIR"] = str(tmp)

    from agent.tools_bridge import mcp_tools  # noqa: E402
    from mcp_server import audit, service  # noqa: E402

    async with mcp_tools(UID) as tools:
        t = {tool.name: tool for tool in tools}

        print("[1] 工具契约")
        expected = {"query_order", "query_after_sales", "query_sales_analytics",
                    "cancel_order", "submit_refund"}
        check("暴露五个工具", set(t) == expected, str(sorted(t)))
        check("查询类工具不含 user_id 参数",
              all("user_id" not in schema_of(t[n]).get("properties", {})
                  for n in ("query_order", "query_after_sales", "query_sales_analytics")))
        refund_props = schema_of(t["submit_refund"]).get("properties", {})
        check("submit_refund 必填 issue_type", "issue_type" in refund_props)
        check("submit_refund 不接收最终金额参数", "amount" not in refund_props,
              str(sorted(refund_props)))
        check("query_order 声明为只读",
              "只读" in t["query_order"].description)
        check("submit_refund 描述禁止模型定金额",
              "金额不由你决定" in t["submit_refund"].description)

        query = t["query_order"]
        after_sales = t["query_after_sales"]
        analytics = t["query_sales_analytics"]
        cancel = t["cancel_order"]
        refund = t["submit_refund"]

        print("\n[2] 查询订单")
        r = parse(await query.ainvoke({}))
        ids = {o["order_id"] for o in r.get("orders", [])}
        check("只返回本人订单", OTHERS not in ids, str(ids))
        check("返回 7 笔", r.get("count") == 7, str(r.get("count")))

        r = parse(await query.ainvoke({"order_id": OTHERS}))
        check("查他人订单报不存在", r.get("error_code") == "ORDER_NOT_FOUND", str(r))

        r = parse(await query.ainvoke({"order_id": COMPLETED}))
        o = (r.get("orders") or [{}])[0]
        check("返回可退余额", o.get("refundable_balance") == "¥458.00",
              str(o.get("refundable_balance")))

        print("\n[3] 查询售后")
        r = parse(await after_sales.ainvoke({}))
        check("返回售后记录", r.get("ok") is True and r.get("count", 0) >= 2, str(r)[:200])
        check("带用户画像", "profile" in r and r["profile"]["order_count"] == 7, str(r.get("profile")))

        r = parse(await after_sales.ainvoke({"order_id": OTHERS}))
        check("查他人售后被拒", r.get("error_code") == "ORDER_NOT_FOUND", str(r))

        print("\n[4] 售后分析")
        r = parse(await analytics.ainvoke({"period": "ALL", "dimension": "PRODUCT"}))
        check("返回商品排行", bool(r.get("by_product")), str(r)[:200])
        check("排行按金额降序",
              [p["amount_cents"] for p in r.get("by_product", [])]
              == sorted([p["amount_cents"] for p in r.get("by_product", [])], reverse=True))
        check("带中文摘要", bool(r.get("text")))

        r = parse(await analytics.ainvoke({"dimension": "ANOMALY"}))
        check("识别出异常订单", bool(r.get("anomalies")), str(r)[:200])

        print("\n[5] 取消")
        r = parse(await cancel.ainvoke({"order_id": OTHERS}))
        check("取消他人订单被拒", r.get("error_code") == "ORDER_NOT_FOUND", str(r))

        r = parse(await cancel.ainvoke({"order_id": PAID, "reason": "NO_LONGER_NEEDED"}))
        check("本人订单取消成功", r.get("ok") is True, str(r))

        r = parse(await cancel.ainvoke({"order_id": PAID}))
        check("重复取消被拒", r.get("error_code") == "ALREADY_CANCELLED", str(r))

        print("\n[6] 未发货 -> 规则引擎自动放款")
        r = parse(await refund.ainvoke({
            "order_id": UNSHIPPED, "reason": "拍错了", "issue_type": "WRONG_ITEM",
            "requested_amount": 99999,
        }))
        check("超额主张被拒", r.get("error_code") == "AMOUNT_EXCEEDED", str(r))

        r = parse(await refund.ainvoke({
            "order_id": UNSHIPPED, "reason": "拍错了", "issue_type": "WRONG_ITEM",
            "requested_amount": 300,
        }))
        check("部分退款立即生效", r.get("result") == "REFUNDED", str(r))
        check("通道为自动放款", r.get("decision", {}).get("channel") == "AUTO_APPROVE",
              str(r.get("decision")))
        check("累计已退 300", r.get("order", {}).get("refunded_amount") == "¥300.00",
              str(r.get("order", {}).get("refunded_amount")))

        r = parse(await refund.ainvoke({
            "order_id": UNSHIPPED, "reason": "退剩余", "issue_type": "OTHER",
        }))
        check("退剩余全部", r.get("order", {}).get("refunded_amount") == "¥699.00", str(r))
        check("退满后置为已退款", r.get("order", {}).get("status") == "REFUNDED", str(r))

        r = parse(await refund.ainvoke({
            "order_id": UNSHIPPED, "reason": "再退", "issue_type": "OTHER",
        }))
        check("已退完不能再退", r.get("error_code") == "ALREADY_REFUNDED", str(r))

        print("\n[7] 已发货 / 已签收 -> 转人工")
        r = parse(await refund.ainvoke({
            "order_id": SHIPPED, "reason": "有杂音", "issue_type": "QUALITY",
            "requested_amount": 500,
        }))
        check("已发货走申请", r.get("result") == "PENDING", str(r))
        check("通道为人工审核", r.get("decision", {}).get("channel") == "MANUAL_REVIEW", str(r))
        check("返回排队位置", isinstance(r.get("queue_position"), int),
              str(r.get("queue_position")))
        check("申请阶段不改订单金额",
              r.get("order", {}).get("refunded_amount") == "¥0.00",
              str(r.get("order", {}).get("refunded_amount")))

        r2 = parse(await refund.ainvoke({
            "order_id": SHIPPED, "reason": "再来", "issue_type": "QUALITY",
        }))
        check("重复申请被拒", r2.get("error_code") == "PENDING_REFUND_EXISTS", str(r2))

        r3 = parse(await refund.ainvoke({
            "order_id": COMPLETED, "reason": "键帽松动", "issue_type": "QUALITY",
        }))
        check("已签收订单可申请", r3.get("result") == "PENDING", str(r3))

        print("\n[8] 商家审批（服务层，与 Web 后台同一入口）")
        queue = service.pending_queue()
        check("队列含三条申请", len(queue) == 3, str(len(queue)))
        check("排队位置连续", [q["queue_position"] for q in queue] == [1, 2, 3],
              str([q["queue_position"] for q in queue]))
        check("队列带风险等级", all(q.get("risk_label") for q in queue),
              str([q.get("risk_label") for q in queue]))

        rid = r.get("refund_id")
        out = service.approve_refund(rid, MERCHANT, note="核实后同意")
        check("审批通过", out.get("ok") is True, str(out))
        check("订单累计已退 500",
              out.get("order", {}).get("refunded_amount") == "¥500.00",
              str(out.get("order", {}).get("refunded_amount")))

        detail = service.review_detail(rid)
        check("审核详情含判断依据", bool(detail["decision"]["rules"]))
        check("审核详情含审核记录", detail["refund"]["review_history"][-1]["by"] == "陈明")

        r4 = parse(await refund.ainvoke({
            "order_id": SHIPPED, "reason": "再退一点", "issue_type": "QUALITY",
            "requested_amount": 9999,
        }))
        check("审批后再申请超额被拒", r4.get("error_code") == "AMOUNT_EXCEEDED", str(r4))

        print("\n[9] 驳回锁定与解锁")
        r5 = parse(await refund.ainvoke({
            "order_id": SHIPPED, "reason": "小额补偿", "issue_type": "QUALITY",
            "requested_amount": 100,
        }))
        rid2 = r5.get("refund_id")
        check("可再次提交小额申请", bool(rid2), str(r5))

        service.reject_refund(rid2, MERCHANT, note="证据不足")
        r6 = parse(await refund.ainvoke({
            "order_id": SHIPPED, "reason": "再试", "issue_type": "QUALITY",
        }))
        check("驳回后订单锁定", r6.get("error_code") == "REFUND_LOCKED_AFTER_REJECT", str(r6))

        service.unlock_order(SHIPPED, MERCHANT, note="复核通过")
        r7 = parse(await refund.ainvoke({
            "order_id": SHIPPED, "reason": "复核后重提", "issue_type": "QUALITY",
            "requested_amount": 100,
        }))
        check("解锁后可重新申请", r7.get("result") == "PENDING", str(r7))

        print("\n[10] 审计留痕")
        st = audit.stats()
        check("记录了规则判定", any(
            e["kind"] == audit.KIND_RULE_DECISION for e in audit.list_events(limit=200)))
        check("记录了人工操作", st["human_actions"] >= 2, str(st))
        check("记录了工具调用之外的业务事件", st["total"] > 0, str(st))
        actions = {e["payload"].get("action") for e in
                   audit.list_events(kind=audit.KIND_HUMAN_ACTION, limit=50)}
        check("审批与驳回都可追溯", {"APPROVE", "REJECT", "UNLOCK"} <= actions, str(actions))

    print("\n" + "=" * 42)
    print(f"通过 {passed} 项，失败 {failed} 项")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
