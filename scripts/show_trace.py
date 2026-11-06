"""查看退款记录与审批链路。

用法:
    python scripts/show_trace.py
    python scripts/show_trace.py --order ORD20261008009
    python scripts/show_trace.py --user lina
    python scripts/show_trace.py --status PENDING
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

from mcp_server import service, store  # noqa: E402

SHIP_LABEL = {
    "SHIPPED": "已发货", "NOT_SHIPPED": "未发货", "DELIVERED": "已签收",
}
MODE_LABEL = {"AUTO": "自动放款", "MANUAL": "人工审批"}


def resolve_user(raw: str) -> str:
    """允许传账号名或 user_id。"""
    users = store.read_only(store.USERS_FILE, store.empty_users())["users"]
    for u in users.values():
        if u["username"] == raw or u["user_id"] == raw:
            return u["user_id"]
    return raw


def main() -> None:
    parser = argparse.ArgumentParser(description="查看退款记录")
    parser.add_argument("--order", help="只看某笔订单")
    parser.add_argument("--user", help="只看某个用户（账号名或 user_id）")
    parser.add_argument("--status", choices=["PENDING", "APPROVED", "REJECTED"],
                        help="只看某个状态")
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    user_id = resolve_user(args.user) if args.user else None
    rows = service.list_refunds(
        order_id=args.order, user_id=user_id, status=args.status, limit=args.limit
    )
    if not rows:
        print("没有符合条件的退款记录")
        return

    locks = {lk["order_id"]: lk for lk in service.list_locks()}

    print(f"共 {len(rows)} 条退款记录\n")
    for r in rows:
        print(f"{r['refund_id']}  {r['order_id']}  {r['status_label']}")
        print(f"  用户 {r['user_name']}   商品 {r['item_name']}")
        print(
            f"  订单实付 {r['order_amount']}   本次 {r['amount']}"
            f"   累计已退 {r['order_refunded']}"
        )
        print(f"  原因: {r['reason']}")
        print(
            f"  方式: {MODE_LABEL.get(r['mode'], r['mode'])}"
            f"   物流: {SHIP_LABEL.get(r['shipping_state'], r['shipping_state'])}"
        )
        if r["reviewer_name"]:
            print(f"  审批人: {r['reviewer_name']}   备注: {r['review_note'] or '—'}")
        print(f"  提交 {r['created_at']}   处理 {r['reviewed_at'] or '未处理'}")

        lock = locks.get(r["order_id"])
        if lock:
            print(f"  锁定中: {lock['reason']}（{lock['locked_at']}，待解锁）")
        print()


if __name__ == "__main__":
    main()
