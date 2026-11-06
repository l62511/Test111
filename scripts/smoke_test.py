"""协议层冒烟测试：完全走 MCP 协议调用三个工具，不经过大模型。

验证的是「Agent 之外」这一整条链路是否真的按规则工作：
    MCP Client -> stdio -> MCP Server -> 规则引擎 -> JSON 存储

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
UNSHIPPED = "ORD20261015002"        # 已取消，未发货 -> 直接退
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
    from mcp_server import service  # noqa: E402

    async with mcp_tools(UID) as tools:
        t = {tool.name: tool for tool in tools}

        print("[1] 工具契约")
        check("暴露三个工具", set(t) == {"query_order", "cancel_order", "refund_order"},
              str(sorted(t)))
        check("query_order 无 user_id 参数",
              "user_id" not in schema_of(t["query_order"]).get("properties", {}))
        check("refund_order 无 approval_id 参数",
              "approval_id" not in schema_of(t["refund_order"]).get("properties", {}))

        query = t["query_order"]
        cancel = t["cancel_order"]
        refund = t["refund_order"]

        print("\n[2] 查询")
        r = parse(await query.ainvoke({}))
        ids = {o["order_id"] for o in r.get("orders", [])}
        check("只返回本人订单", OTHERS not in ids, str(ids))
        check("返回 6 笔", r.get("count") == 6, str(r.get("count")))

        r = parse(await query.ainvoke({"order_id": OTHERS}))
        check("查他人订单报不存在", r.get("error_code") == "ORDER_NOT_FOUND", str(r))

        r = parse(await query.ainvoke({"order_id": COMPLETED}))
        o = (r.get("orders") or [{}])[0]
        check("返回可退余额", o.get("refundable_balance") == "¥458.00",
              str(o.get("refundable_balance")))

        print("\n[3] 取消")
        r = parse(await cancel.ainvoke({"order_id": OTHERS}))
        check("取消他人订单被拒", r.get("error_code") == "ORDER_NOT_FOUND", str(r))

        r = parse(await cancel.ainvoke({"order_id": PAID, "reason": "NO_LONGER_NEEDED"}))
        check("本人订单取消成功", r.get("ok") is True, str(r))

        r = parse(await cancel.ainvoke({"order_id": PAID}))
        check("重复取消被拒", r.get("error_code") == "ALREADY_CANCELLED", str(r))

        print("\n[4] 未发货直接退款")
        r = parse(await refund.ainvoke({"order_id": UNSHIPPED, "amount": 99999}))
        check("超额退款被拒", r.get("error_code") == "AMOUNT_EXCEEDED", str(r))

        r = parse(await refund.ainvoke({"order_id": UNSHIPPED, "amount": 300, "reason": "拍错"}))
        check("部分退款立即生效", r.get("result") == "REFUNDED", str(r))
        check("累计已退 300", r.get("order", {}).get("refunded_amount") == "¥300.00",
              str(r.get("order", {}).get("refunded_amount")))

        r = parse(await refund.ainvoke({"order_id": UNSHIPPED, "reason": "退剩余"}))
        check("退剩余全部", r.get("order", {}).get("refunded_amount") == "¥699.00", str(r))
        check("退满后置为已退款", r.get("order", {}).get("status") == "REFUNDED", str(r))

        r = parse(await refund.ainvoke({"order_id": UNSHIPPED}))
        check("已退完不能再退", r.get("error_code") == "ALREADY_REFUNDED", str(r))

        print("\n[5] 已发货 / 已签收 -> 提交申请")
        r = parse(await refund.ainvoke({"order_id": SHIPPED, "amount": 500, "reason": "有杂音"}))
        check("已发货走申请", r.get("result") == "PENDING", str(r))
        check("返回排队位置", isinstance(r.get("queue_position"), int),
              str(r.get("queue_position")))
        check("申请阶段不改订单金额",
              r.get("order", {}).get("refunded_amount") == "¥0.00",
              str(r.get("order", {}).get("refunded_amount")))

        r2 = parse(await refund.ainvoke({"order_id": SHIPPED, "amount": 100}))
        check("重复申请被拒", r2.get("error_code") == "PENDING_REFUND_EXISTS", str(r2))

        r3 = parse(await refund.ainvoke({"order_id": COMPLETED, "reason": "键帽松动"}))
        check("已签收订单可申请", r3.get("result") == "PENDING", str(r3))

        print("\n[6] 商家审批（服务层，与 Web 后台同一入口）")
        queue = service.pending_queue()
        check("队列含三条申请", len(queue) == 3, str(len(queue)))
        check("排队位置连续", [q["queue_position"] for q in queue] == [1, 2, 3],
              str([q["queue_position"] for q in queue]))

        rid = r.get("refund_id")
        out = service.approve_refund(rid, MERCHANT, note="核实后同意")
        check("审批通过", out.get("ok") is True, str(out))
        check("订单累计已退 500",
              out.get("order", {}).get("refunded_amount") == "¥500.00",
              str(out.get("order", {}).get("refunded_amount")))

        r4 = parse(await refund.ainvoke({"order_id": SHIPPED, "amount": 9999}))
        check("审批后再申请超额被拒", r4.get("error_code") == "AMOUNT_EXCEEDED", str(r4))

        print("\n[7] 驳回锁定与解锁")
        r5 = parse(await refund.ainvoke({"order_id": SHIPPED, "amount": 100}))
        rid2 = r5.get("refund_id")
        check("可再次提交小额申请", bool(rid2), str(r5))

        service.reject_refund(rid2, MERCHANT, note="证据不足")
        r6 = parse(await refund.ainvoke({"order_id": SHIPPED, "amount": 100}))
        check("驳回后订单锁定", r6.get("error_code") == "REFUND_LOCKED_AFTER_REJECT", str(r6))

        service.unlock_order(SHIPPED, MERCHANT, note="复核通过")
        r7 = parse(await refund.ainvoke({"order_id": SHIPPED, "amount": 100}))
        check("解锁后可重新申请", r7.get("result") == "PENDING", str(r7))

    print("\n" + "=" * 42)
    print(f"通过 {passed} 项，失败 {failed} 项")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
