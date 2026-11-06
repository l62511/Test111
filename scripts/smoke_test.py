"""协议层冒烟测试：完全走 MCP 协议调用三个工具，不经过大模型。

验证的是「Agent 之外」这一整条链路是否真的按规则工作：
    MCP Client -> stdio -> MCP Server -> 规则引擎 -> SQLite

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

MINE = "ORD20261028001"      # 张伟的已支付、未发货订单
MINE_CANCELLED = "ORD20261015002"  # 张伟的已取消、未发货订单
SHIPPED = "ORD20261008009"   # 张伟的已取消、已发货订单（退款需审批）
OTHERS = "ORD20261103006"    # 李娜的订单

passed = 0
failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label}  {detail}")


def parse(result) -> dict:
    """MCP 工具返回的是 content 列表，取其中的 JSON。"""
    if isinstance(result, dict):
        return result
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except json.JSONDecodeError:
                    continue
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {"raw": result}
    return {"raw": str(result)}


async def main() -> None:
    tmp = Path(tempfile.mkdtemp()) / "smoke.db"
    seed(tmp)
    os.environ["ORDER_DB_PATH"] = str(tmp)
    os.environ["CALLER_ID"] = "u_1001"

    from agent.tools_bridge import load_tools

    tools = {t.name: t for t in await load_tools()}
    check("三个工具全部经 MCP 暴露", set(tools) == {"query_order", "cancel_order", "refund_order"},
          str(set(tools)))

    query = tools["query_order"]
    cancel = tools["cancel_order"]
    refund = tools["refund_order"]

    print("\n[1] 查询")
    r = parse(await query.ainvoke({"limit": 10}))
    check("能查出本人全部订单", r.get("count") == 6, str(r.get("count")))
    check("查询结果不含他人订单",
          all(o["order_id"] != OTHERS for o in r.get("orders", [])))

    r = parse(await query.ainvoke({"order_id": OTHERS}))
    check("查他人订单返回 ORDER_NOT_FOUND", r.get("error_code") == "ORDER_NOT_FOUND", str(r))

    print("\n[2] 取消")
    r = parse(await query.ainvoke({"order_id": MINE}))
    check("已支付订单可取消", r["orders"][0]["can_cancel"] is True)

    r = parse(await cancel.ainvoke({"order_id": OTHERS}))
    check("取消他人订单被拒绝", r.get("error_code") == "ORDER_NOT_FOUND", str(r))

    r = parse(await cancel.ainvoke({"order_id": MINE, "reason": "NO_LONGER_NEEDED"}))
    check("取消本人已支付订单成功", r.get("ok") is True, str(r))

    r = parse(await cancel.ainvoke({"order_id": MINE}))
    check("重复取消被拒绝", r.get("error_code") == "ALREADY_CANCELLED", str(r))

    print("\n[3] 未发货退款（直接放款）")
    r = parse(await refund.ainvoke({"order_id": "ORD20261101004"}))
    check("未取消订单退款被拒绝", r.get("error_code") == "ORDER_NOT_CANCELLED", str(r))

    r = parse(await refund.ainvoke({"order_id": MINE_CANCELLED, "amount": 99999}))
    check("超额退款被拒绝", r.get("error_code") == "AMOUNT_EXCEEDED", str(r))

    r = parse(await refund.ainvoke({"order_id": MINE, "reason": "拍错了"}))
    check("已取消订单退款成功", r.get("ok") is True, str(r))
    check("未发货不走审批", r.get("approval") == "NOT_REQUIRED", str(r.get("approval")))
    check("退款金额等于实付", r.get("order", {}).get("refund_amount") == "¥2,499.00",
          str(r.get("order", {}).get("refund_amount")))

    r = parse(await refund.ainvoke({"order_id": MINE}))
    check("重复退款被拒绝", r.get("error_code") == "ALREADY_REFUNDED", str(r))

    print("\n[4] 已发货退款（人工审批）")
    r = parse(await refund.ainvoke({"order_id": SHIPPED, "reason": "商品有破损"}))
    check("已发货先返回需审批", r.get("error_code") == "MANUAL_APPROVAL_REQUIRED", str(r))
    approval_id = r.get("approval_id")
    check("返回审批凭证", bool(approval_id), str(r))
    check("带出发货状态", r.get("shipping_state") == "SHIPPED", str(r.get("shipping_state")))

    q = parse(await query.ainvoke({"order_id": SHIPPED}))
    check("审批前订单未退款", q["orders"][0]["status"] == "CANCELLED",
          q["orders"][0]["status"])
    check("查询侧标记需审批", q["orders"][0]["refund_needs_approval"] is True)

    r = parse(await refund.ainvoke({"order_id": SHIPPED, "approval_id": approval_id}))
    check("审批通过后放款成功", r.get("ok") is True, str(r))
    check("标记为人工审批通过", r.get("approval") == "APPROVED", str(r.get("approval")))

    r = parse(await refund.ainvoke({"order_id": SHIPPED, "approval_id": approval_id}))
    check("同一审批单不能重复放款",
          r.get("error_code") in {"ALREADY_REFUNDED", "INVALID_APPROVAL"}, str(r))

    print("\n[5] 退款追踪")
    from mcp_server import service as svc

    traces = svc.list_refund_traces(order_id=MINE)
    check("自动退款留下追踪记录", len(traces) == 1, str(len(traces)))
    check("追踪记录退款原因", traces and traces[0]["reason"] == "拍错了",
          str(traces[0]["reason"] if traces else None))
    check("追踪记录审批方式为 AUTO", traces and traces[0]["approval_mode"] == "AUTO")

    traces = svc.list_refund_traces(order_id=SHIPPED)
    check("审批退款留下追踪记录", len(traces) == 1, str(len(traces)))
    check("追踪记录审批方式为 MANUAL", traces and traces[0]["approval_mode"] == "MANUAL")
    check("追踪记录审批状态 APPROVED", traces and traces[0]["approval_status"] == "APPROVED")
    check("追踪记录退款原因", traces and traces[0]["reason"] == "商品有破损",
          str(traces[0]["reason"] if traces else None))
    check("追踪记录审批人", bool(traces and traces[0]["approver"]))

    print("\n[6] 状态一致性")
    r = parse(await query.ainvoke({"order_id": MINE}))
    order = r["orders"][0]
    check("最终状态为已退款", order["status"] == "REFUNDED", order["status"])
    check("终态下不可再取消/退款",
          order["can_cancel"] is False and order["can_refund"] is False)

    print(f"\n{'=' * 42}\n通过 {passed} 项，失败 {failed} 项")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
