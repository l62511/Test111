"""查看退款追踪记录（refund_trace）。

用法:
    python scripts/show_trace.py
    python scripts/show_trace.py --order ORD20261008009
    python scripts/show_trace.py --full
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

load_dotenv(BASE_DIR / ".env")

from mcp_server import service  # noqa: E402

MODE_LABEL = {"AUTO": "自动放款", "MANUAL": "人工审批"}
STATE_LABEL = {
    "NOT_REQUIRED": "无需审批",
    "PENDING": "待审批",
    "APPROVED": "已通过",
    "REJECTED": "已驳回",
}
SHIP_LABEL = {"SHIPPED": "已发货", "NOT_SHIPPED": "未发货"}


def main() -> None:
    parser = argparse.ArgumentParser(description="查看退款追踪记录")
    parser.add_argument("--order", help="只看某笔订单")
    parser.add_argument("--caller", help="只看某个用户")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--full", action="store_true", help="显示完整字段")
    args = parser.parse_args()

    rows = service.list_refund_traces(
        order_id=args.order, caller_id=args.caller, limit=args.limit
    )
    if not rows:
        print("没有匹配的退款追踪记录")
        return

    # 把驳回锁一并取出来，按订单归组，让一条命令能看完整链路
    locks: dict[str, list[dict]] = {}
    for lk in service.list_locks(order_id=args.order, limit=100):
        locks.setdefault(lk["order_id"], []).append(lk)

    print(f"共 {len(rows)} 条退款追踪记录\n")
    for r in rows:
        print(f"{r['trace_id']}  {r['order_id']}")
        print(
            f"  金额 {r['amount']}   {SHIP_LABEL.get(r['shipping_state'], r['shipping_state'])}"
            f"   {MODE_LABEL.get(r['approval_mode'], r['approval_mode'])}"
            f"   {STATE_LABEL.get(r['approval_status'], r['approval_status'])}"
            f"   结果 {r['outcome']}"
        )
        print(f"  原因: {r['reason']}")
        if r["approver"]:
            print(f"  审批人: {r['approver']}")

        order_locks = locks.get(r["order_id"], [])
        active = [x for x in order_locks if x["active"]]
        if active:
            print(f"  锁定状态: 生效中（{active[0]['locked_at']} 驳回，待运营解锁）")
        elif order_locks:
            last = order_locks[0]
            print(f"  锁定状态: 已解锁（{last['unlocked_by']} @ {last['unlocked_at']}）")
            if args.full and last["unlock_note"]:
                print(f"  解锁备注: {last['unlock_note']}")

        if args.full:
            print(f"  提交人: {r['caller_id']}")
            print(f"  备注: {r['note']}")
            print(f"  创建: {r['created_at']}   更新: {r['updated_at']}")
        print()


if __name__ == "__main__":
    main()
